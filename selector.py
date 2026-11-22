"""Blast Radius selector.

For each patch, decide which sqlglot tests the change can affect, then pack them
onto workers. Every decision comes from maps built at the pinned commit
(per-test line coverage, files each test opens, dialect and expression usage),
which the track rules allow:

- A hunk that only adds or removes comments or blank lines outside strings
  changes nothing Python executes, so it selects nothing on its own.
- A changed statement inside a function selects the tests that executed it.
- A changed class attribute selects the tests that used that class: the dialects
  it serves, or the expression type and its subclasses.
- A changed fixture or data file selects the tests that opened it. For the TPC-H and
  TPC-DS query files, whose readers loop over cases, the old and new files are split
  into cases by the tests' own load_sql_fixture_pairs, and only readers of a changed
  case are selected; test_execute_tpc* is then cleared if each changed case passes
  the test's own steps when re-run here.
- Anything we can't reason about (a patch that won't apply, a syntax error, a
  module-level change other modules import, config files) runs every test.

Logs go to stderr. stdout holds exactly one JSON line per commit.
"""

import ast
import difflib
import gzip
import heapq
import json
import math
import os
import pickle
import re
import sys
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/opt/sqlglot"))
# the pinned commit's top-level config files (pyproject.toml, setup.cfg, setup.py), for judging edits to them
ROOT_FILES = Path(os.environ.get("BR_ROOT_FILES", "/opt/sqlglot_root"))
MAP = Path(os.environ.get("BR_MAP", str(Path(__file__).with_name("blast_map.pkl.gz"))))
TPCDS_DATA = Path(os.environ.get("BR_TPCDS", str(Path(__file__).with_name("tpcds.pkl.gz"))))
GLOBALS_DATA = Path(os.environ.get("BR_GLOBALS", str(Path(__file__).with_name("globals_refs.pkl.gz"))))
GENMEM_DATA = Path(os.environ.get("BR_GENMEM", str(Path(__file__).with_name("genmem.pkl.gz"))))
DICTKEYS_DATA = Path(os.environ.get("BR_DICTKEYS", str(Path(__file__).with_name("dictkeys.pkl.gz"))))
CMP_DATA = Path(os.environ.get("BR_CMP", str(Path(__file__).with_name("cmp.pkl.gz"))))
ARGREQ_DATA = Path(os.environ.get("BR_ARGREQ", str(Path(__file__).with_name("argreq.pkl.gz"))))
IMPORTS_DATA = Path(os.environ.get("BR_IMPORTS", str(Path(__file__).with_name("import_users.pkl.gz"))))
OPERANDS_DATA = Path(os.environ.get("BR_OPERANDS", str(Path(__file__).with_name("operands.pkl.gz"))))
DICTREADS_DATA = Path(os.environ.get("BR_DICTREADS", str(Path(__file__).with_name("dictreads.pkl.gz"))))
DEFAULTS_DATA = Path(os.environ.get("BR_DEFAULTS", str(Path(__file__).with_name("defaults.pkl.gz"))))
ATTR_OWNERS_DATA = Path(os.environ.get("BR_ATTR_OWNERS", str(Path(__file__).with_name("attr_owners.pkl.gz"))))
TPCDS_ENV_DATA = Path(os.environ.get("BR_TPCDS_ENV", str(Path(__file__).with_name("tpcds_env.pkl.gz"))))
ENV_FILE = "sqlglot/executor/env.py"
TPCDS_CMP_DATA = Path(os.environ.get("BR_TPCDS_CMP", str(Path(__file__).with_name("tpcds_cmp.pkl.gz"))))
IMPORT_LAMBDAS_DATA = Path(os.environ.get("BR_IMPORT_LAMBDAS", str(Path(__file__).with_name("import_lambdas.pkl.gz"))))
CMP_NAMES = {ast.Is: "is", ast.IsNot: "is not", ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=",
             ast.Gt: ">", ast.GtE: ">=", ast.In: "in", ast.NotIn: "not in"}
CMP_ALT = {"is": "==", "is not": "!=", "<": "<=", "<=": "<", ">": ">=", ">=": ">"}  # as tools/cmp_plugin.py
GENMEM_TEST = "tests/test_generator.py::TestGenerator::test_generator_memory"
# file -> (function that builds a per-process cache on first use, the cache's name)
FIRST_USE = {"sqlglot/generator.py": ("_build_dispatch", "_DISPATCH_CACHE")}
TPCDS_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
TPCH_EXEC_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpch"
# The SQL fixtures whose readers loop over cases, and what each reader takes from every case
# (meta, sql, expected), as read from the tests at the pinned commit:
#   "pair"  test_optimizer's check_file: the whole case (meta keys, input, expected output)
#   "sql_expected"  test_optimized_tpch: duckdb runs the input and the expected output
#   "exec"  test_execute_tpc*: parse_one(input) for every case, and for cases whose meta says
#           execute, the Python executor's rows against duckdb's (subtestHelper)
# Every other test of TestExecutor only needs setUpClass, which parses both files with
# load_sql_fixture_pairs; any other reader of the file keeps the file-level rule.
FIXTURE_VIEWS = {
    "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql": {
        "tests/test_optimizer.py::TestOptimizer::test_tpcds": "pair",
        TPCDS_TEST: "exec",
    },
    "tests/fixtures/optimizer/tpc-h/tpc-h.sql": {
        "tests/test_optimizer.py::TestOptimizer::test_tpch": "pair",
        "tests/test_executor.py::TestExecutor::test_optimized_tpch": "sql_expected",
        TPCH_EXEC_TEST: "exec",
    },
}
FIXTURE_LOAD_CLASS = "tests/test_executor.py::TestExecutor::"
# test sources whose change could change how the cases above are read or used
FIXTURE_READER_FILES = {"tests/helpers.py", "tests/test_executor.py", "tests/test_optimizer.py", "tests/__init__.py"}
FIXTURE_GATE = 0.4  # a case check is planned only if its CPU estimate is at most this share of the saving
TPCDS_TEST_MODULES = {"tests/__init__.py", "tests/test_executor.py", "tests/helpers.py"}  # its module and what it imports
# Plan a TPC-DS replay only if its estimated CPU is at most this share of the time it would
# save. Full replays run the executor on changed code, where outputs often differ late and
# changed plans can run slower, so they get a stricter gate: on the scorer's machines they
# take about 1.3x the recorded estimate, and the board bills CPU at about twice that, so a
# full replay estimated at 10 s that passes costs more than the 21.5 s test it drops.
TPCDS_GATE = {"front": 0.8, "full": 0.35}
TPCDS_BUDGET = 0.85  # the replay child is stopped (and the test kept) past this share of the saving
TPCDS_RERUN_SHARE = float(os.environ.get("BR_RERUN_SHARE", "0.5"))  # second front pass only if its estimate fits in this share of what is left
TPCDS_EARLY_RERUNS = 3  # changed plans run to the end at once, before deferring the rest
WORKER_MS = 2000
MAX_WORKERS = 8

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match)
if hasattr(ast, "TryStar"):
    COMPOUND = COMPOUND + (ast.TryStar,)
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def log(*args):
    print(*args, file=sys.stderr)


class Fallback(Exception):
    """Give up on precision for this commit and run every test."""


# --------------------------------------------------------------------------- patches


def parse_patch(text):
    lines = text.split("\n")
    diffs, cur, i, n = [], None, 0, len(lines)

    def new_diff():
        d = {"old": None, "new": None, "hunks": [], "binary": False, "new_file": False, "deleted": False,
             "mode": False}
        diffs.append(d)
        return d

    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            cur = new_diff()
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                cur["old"], cur["new"] = m.group(1), m.group(2)
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or cur["hunks"]:
                cur = new_diff()
            old = line[4:].split("\t")[0]
            new = lines[i + 1][4:].split("\t")[0]
            cur["old"] = None if old == "/dev/null" else (old[2:] if old.startswith("a/") else old)
            cur["new"] = None if new == "/dev/null" else (new[2:] if new.startswith("b/") else new)
            cur["new_file"] = cur["new_file"] or old == "/dev/null"
            cur["deleted"] = cur["deleted"] or new == "/dev/null"
            i += 2
            continue
        m = HUNK_RE.match(line)
        if m and cur is not None:
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) is not None else 1
            c = int(m.group(3))
            d = int(m.group(4)) if m.group(4) is not None else 1
            body, left_old, left_new = [], b, d
            i += 1
            while i < n and (left_old > 0 or left_new > 0):
                piece = lines[i]
                if piece.startswith("\\"):
                    i += 1
                    continue
                tag = piece[:1] if piece else " "
                if tag == " ":
                    left_old -= 1
                    left_new -= 1
                elif tag == "-":
                    left_old -= 1
                elif tag == "+":
                    left_new -= 1
                else:
                    break
                body.append(piece if piece else " ")
                i += 1
            while i < n and lines[i].startswith("\\"):
                i += 1
            cur["hunks"].append((a, b, body))
            continue
        if cur is not None:
            if line.startswith("new file mode"):
                cur["new_file"] = True
            elif line.startswith("deleted file mode"):
                cur["deleted"] = True
            elif line.startswith(("old mode ", "new mode ")):
                cur["mode"] = True
            elif line.startswith(("Binary files", "GIT binary patch")):
                cur["binary"] = True
            elif line.startswith("rename from "):
                cur["old"] = line[len("rename from "):]
            elif line.startswith("rename to "):
                cur["new"] = line[len("rename to "):]
        i += 1
    return diffs


def apply_hunks(old_text, hunks):
    """Apply hunks; return the new text and, per hunk, its change blocks in old/new line numbers."""
    old = old_text.split("\n")
    out, pos, offset, records = [], 0, 0, []
    for a, b, body in hunks:
        old_seg = [p[1:] for p in body if p[:1] in (" ", "-")]
        new_seg = [p[1:] for p in body if p[:1] in (" ", "+")]
        start = a - 1 if b > 0 else a
        guess = start + offset

        def fits(s):
            return 0 <= s and old[s:s + len(old_seg)] == old_seg

        s = guess if fits(guess) else None
        if s is None:
            for delta in range(1, 300):
                if fits(guess - delta):
                    s = guess - delta
                    break
                if fits(guess + delta):
                    s = guess + delta
                    break
        if s is None or s < pos:
            raise Fallback("hunk does not apply")
        out.extend(old[pos:s])
        new_start = len(out)
        out.extend(new_seg)
        pos = s + len(old_seg)
        offset = s - start

        # change blocks: runs of -/+ lines between context lines
        blocks, cur = [], None
        o, nn = s + 1, new_start + 1  # 1-based line numbers
        for p in body:
            tag = p[:1]
            if tag == " ":
                cur = None
                o += 1
                nn += 1
                continue
            if cur is None:
                cur = {"removed": [], "added": [], "anchor": o, "removed_text": [], "added_text": []}
                blocks.append(cur)
            if tag == "-":
                cur["removed"].append(o)
                cur["removed_text"].append(p[1:])
                o += 1
            else:
                cur["added"].append(nn)
                cur["added_text"].append(p[1:])
                nn += 1
        records.extend(blocks)
    out.extend(old[pos:])
    return "\n".join(out), records


# --------------------------------------------------------------------------- source index


class SourceIndex:
    """Statements of one Python file with the lines that belong to each."""

    def __init__(self, text):
        self.tree = ast.parse(text.encode("utf-8", "surrogateescape"))
        self.lines = text.split("\n")
        self.stmts = []  # dicts
        self._in_string = {}
        self.continued = set()
        for idx, line in enumerate(self.lines, start=1):
            if line.rstrip().endswith("\\"):
                self.continued.add(idx)
        self._walk(self.tree.body, scope="module", cls=None, depth=0, func=None)
        nlines = len(self.lines) + 2
        self.own_at = [None] * nlines
        self.full_at = [None] * nlines
        for i, st in enumerate(self.stmts):
            for ln in range(st["own"][0], min(st["own"][1], nlines - 1) + 1):
                cur = self.own_at[ln]
                if cur is None or self.stmts[cur]["depth"] <= st["depth"]:
                    self.own_at[ln] = i
            for ln in range(st["full"][0], min(st["full"][1], nlines - 1) + 1):
                cur = self.full_at[ln]
                if cur is None or self.stmts[cur]["depth"] <= st["depth"]:
                    self.full_at[ln] = i

    def _walk(self, body, scope, cls, depth, func):
        for node in body:
            self._add(node, scope, cls, depth, func)

    def _add(self, node, scope, cls, depth, func):
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        end = node.end_lineno or node.lineno
        if isinstance(node, (ast.ClassDef,) + DEFS + COMPOUND):
            first_body = node.body[0].lineno if getattr(node, "body", None) else node.lineno + 1
            own = (start, max(node.lineno, first_body - 1))
        else:
            own = (start, end)
        kind = "class" if isinstance(node, ast.ClassDef) else "def" if isinstance(node, DEFS) else "stmt"
        self.stmts.append({"own": own, "full": (start, end), "depth": depth, "scope": scope,
                           "cls": cls, "kind": kind, "node": node, "func": func})
        if isinstance(node, ast.ClassDef):
            qual = f"{cls}.{node.name}" if cls and scope == "class" else node.name
            self._walk(node.body, scope="class" if scope != "function" else "function", cls=qual, depth=depth + 1, func=func)
        elif isinstance(node, DEFS):
            self._walk(node.body, scope="function", cls=cls, depth=depth + 1, func=node)
        else:
            for field in ("body", "orelse", "finalbody"):
                self._walk(getattr(node, field, []) or [], scope, cls, depth + 1, func)
            for handler in getattr(node, "handlers", []) or []:
                hstart = handler.lineno
                hbody = handler.body[0].lineno if handler.body else hstart + 1
                self.stmts.append({"own": (hstart, max(hstart, hbody - 1)), "full": (hstart, handler.end_lineno or hstart),
                                   "depth": depth + 1, "scope": scope, "cls": cls, "kind": "stmt", "node": handler, "func": func})
                self._walk(handler.body, scope, cls, depth + 2, func)
            for case in getattr(node, "cases", []) or []:
                self._walk(case.body, scope, cls, depth + 2, func)

    BODY_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")

    def in_string(self, line):
        """Whether `line` lies inside a string literal that starts on an earlier line. Only
        the statement owning the line is searched (a compound statement's header, not its
        body): a string spanning the line belongs to it."""
        hit = self._in_string.get(line)
        if hit is not None:
            return hit
        hit = False
        i = self.own_at[line] if 0 < line < len(self.own_at) else None
        if i is not None:
            todo = [self.stmts[i]["node"]]
            compound = isinstance(todo[0], (ast.ClassDef, ast.ExceptHandler) + DEFS + COMPOUND)
            first = True
            while todo and not hit:
                node = todo.pop()
                if isinstance(node, (ast.Constant, ast.JoinedStr)) \
                        and isinstance(getattr(node, "value", ""), (str, bytes, list)):
                    end = getattr(node, "end_lineno", None) or node.lineno
                    if node.lineno < line <= end:
                        hit = True
                        break
                for field, value in ast.iter_fields(node):
                    if first and compound and field in self.BODY_FIELDS:
                        continue
                    if isinstance(value, ast.AST):
                        todo.append(value)
                    elif isinstance(value, list):
                        todo.extend(v for v in value if isinstance(v, ast.AST))
                first = False
        self._in_string[line] = hit
        return hit

    def stmt_at(self, line):
        if 0 < line < len(self.own_at):
            i = self.own_at[line]
            if i is not None:
                return self.stmts[i], self.stmts[i]["own"]
            i = self.full_at[line]
            if i is not None:
                return self.stmts[i], self.stmts[i]["full"]
        return None, None

    def class_span(self, qual):
        for st in self.stmts:
            if st["kind"] == "class":
                node = st["node"]
                name = f"{st['cls']}.{node.name}" if st["cls"] and st["scope"] == "class" else node.name
                if name == qual:
                    return st["full"]
        return None

    def class_node(self, qual):
        for st in self.stmts:
            if st["kind"] == "class":
                node = st["node"]
                name = f"{st['cls']}.{node.name}" if st["cls"] and st["scope"] == "class" else node.name
                if name == qual:
                    return node
        return None

    def classes(self):
        out = []
        for st in self.stmts:
            if st["kind"] == "class" and st["scope"] in ("module", "class"):
                node = st["node"]
                out.append(f"{st['cls']}.{node.name}" if st["cls"] and st["scope"] == "class" else node.name)
        return out


def changed_range(old, new):
    """Byte offsets [lo, hi) of the part of `old` that differs from `new`."""
    p, m = 0, min(len(old), len(new))
    while p < m and old[p] == new[p]:
        p += 1
    s = 0
    while s < m - p and old[len(old) - 1 - s] == new[len(new) - 1 - s]:
        s += 1
    return len(old[:p].encode("utf-8")), len(old[: len(old) - s].encode("utf-8"))


def is_comment_or_blank(text):
    s = text.strip()
    return not s or s.startswith("#")


class Blobs:
    """A mapping whose values are stored pickled and unpickled on first use, so loading a data
    file costs little and a run pays only for the entries its commits look at."""

    _MISSING = object()

    def __init__(self, blobs):
        self._blobs = blobs or {}
        self._cache = {}

    def get(self, key, default=None):
        v = self._cache.get(key, self._MISSING)
        if v is self._MISSING:
            b = self._blobs.get(key)
            if b is None:
                return default
            v = self._cache[key] = pickle.loads(b)
        return v

    def __getitem__(self, key):
        v = self.get(key, self._MISSING)
        if v is self._MISSING:
            raise KeyError(key)
        return v

    def __contains__(self, key):
        return key in self._blobs


# --------------------------------------------------------------------------- recorded operands
#
# A second profiled run of the suite at the pinned commit (tools/operand_plugin.py) records,
# per test, the operands each single-operator comparison in sqlglot/ saw, and the keys each
# module- or class-level dict literal gave out. The dict literals are wrapped when they are
# created, so a reference another object keeps to one (the tokenizer holds on to
# NUMERIC_ESCAPES) still records its reads, and reads made while importing are recorded so
# those keys can be refused.
#
# Comparisons: when an edit only swaps a comparison's operator or changes a literal operand,
# the old and new comparison are evaluated on those inputs; a test whose every recorded
# result is unchanged runs exactly as before. Anything we can't evaluate keeps the test.


class Unknown(Exception):
    pass


class Operands:
    def __init__(self, data):
        self.sites = Blobs(data.get("sites_by_file"))  # rel -> {(line, col): site}
        self.enums = data.get("enums", {})
        self.dicts = Blobs(data.get("dicts_by_file"))  # rel -> {(line, col): dict reads}

    def site(self, rel, line, col):
        return (self.sites.get(rel) or {}).get((line, col))

    def dict_at(self, rel, line, col):
        return (self.dicts.get(rel) or {}).get((line, col))

    PURE_VALUE = (ast.Constant, ast.Name, ast.Attribute, ast.Tuple, ast.List, ast.Set, ast.Dict, ast.UnaryOp,
                  ast.BinOp, ast.operator, ast.unaryop, ast.expr_context, ast.Lambda, ast.arguments, ast.arg)

    def dict_readers(self, rel, dict_node, key, old_value, new_value):
        """Tests that read `key` of the module/class-level dict literal `dict_node` (or read
        it in bulk). Unknown if import-time code read it, or the values aren't plain."""
        d = self.dict_at(rel, dict_node.lineno, dict_node.col_offset)
        if d is None or d["import_bulk"]:
            raise Unknown
        for v in (old_value, new_value):
            if not all(isinstance(x, self.PURE_VALUE) for x in ast.walk(v)):
                raise Unknown
        if isinstance(key, ast.Constant) and type(key.value) in (str, int, bytes, float, type(None), bool):
            wanted = {("v", type(key.value).__name__, key.value)}
            match = lambda r: r in wanted
        elif isinstance(key, ast.Attribute):
            # enum member or class keys: match on the member / class name (a superset is safe)
            name = key.attr
            match = lambda r: (r[0] == "e" and r[2] == name) or (r[0] == "c" and r[1].rsplit(".", 1)[-1] == name)
        else:
            raise Unknown
        if any(match(r) or r == ("?",) for r in d["import_keys"]):
            raise Unknown
        m = d["bulk"]
        for r, mask in d["reads"].items():
            if match(r) or r == ("?",):
                m |= mask
        return m

    # ---- abstract evaluation; reps: ("v", type, value) primitives, ("e", enum, member),
    # ("c", class), ("t"|"l"|"s", items), ("o", type, identity_eq)

    @staticmethod
    def identical(a, b):
        """Whether the objects are the same, from their reps alone, or None."""
        if a[0] != b[0]:
            return False  # different kinds are different types, so different objects
        if a[0] == "v":
            if a[1] in ("NoneType", "bool") or b[1] in ("NoneType", "bool"):
                return a == b
            return None
        if a[0] in ("e", "c"):
            return a == b
        return None

    def enum_info(self, rep):
        info = self.enums.get(rep[1])
        if info is None:
            raise Unknown
        return info

    def eq(self, a, b):
        ka, kb = a[0], b[0]
        if ka == "v" and kb == "v":
            return a[2] == b[2]
        if ka == "o" or kb == "o":
            o, other = (a, b) if ka == "o" else (b, a)
            if not o[2] or other[0] == "o":
                raise Unknown
            if other[0] == "e" and not self.enum_info(other)["identity"]:
                raise Unknown
            return False  # default equality is identity, and these are different objects
        if ka == "e" and kb == "e":
            ia, ib = self.enum_info(a), self.enum_info(b)
            if ia["identity"] and ib["identity"]:
                return a == b
            if a[1] == b[1] and ia.get("mixin") in ("str", "int", "bytes"):
                va, vb = ia["members"].get(a[2]), ia["members"].get(b[2])
                if va is None or vb is None or va[0] != "v" or vb[0] != "v":
                    raise Unknown
                return va[2] == vb[2]
            raise Unknown
        if ka == "e" or kb == "e":
            e = a if ka == "e" else b
            if self.enum_info(e)["identity"]:
                return False
            raise Unknown
        if ka == "c" or kb == "c":
            return a == b
        if ka in ("t", "l", "s") and kb in ("t", "l", "s"):
            if ka != kb:
                return False
            if len(a[1]) != len(b[1]):
                return False if ka != "s" else self.set_eq(a, b)
            if ka == "s":
                return self.set_eq(a, b)
            return all(self.eq(x, y) for x, y in zip(a[1], b[1]))
        return False  # a container against a primitive

    @staticmethod
    def set_eq(a, b):
        if all(x[0] == "v" for x in a[1] + b[1]):
            return frozenset(x[2] for x in a[1]) == frozenset(x[2] for x in b[1])
        raise Unknown

    def contains(self, item, box):
        if box[0] == "v":
            if item[0] == "v" and box[1] in ("str", "bytes") and item[1] == box[1]:
                return item[2] in box[2]
            raise Unknown
        if box[0] in ("t", "l"):
            for e in box[1]:
                if self.identical(e, item) is True or self.eq(e, item):
                    return True
            return False
        if box[0] == "s":
            elems = box[1]
            if all(e[0] == "v" for e in elems) and item[0] == "v":
                return item[2] in frozenset(e[2] for e in elems)
            if all(e[0] == "e" and self.enum_info(e)["identity"] for e in elems):
                if item[0] == "e" and self.enum_info(item)["identity"]:
                    return item in elems
                if item[0] in ("v", "c"):
                    return False
            raise Unknown
        raise Unknown

    def evaluate(self, op, a, b, same):
        if op in ("is", "is not"):
            r = same if same is not None else self.identical(a, b)
            if r is None:
                raise Unknown
            return r if op == "is" else not r
        if op in ("==", "!="):
            if a[0] == "o" and b[0] == "o":
                if not (a[2] and b[2]) or same is None:
                    raise Unknown
                r = same
            else:
                r = self.eq(a, b)
            return r if op == "==" else not r
        if op in ("in", "not in"):
            r = self.contains(a, b)
            return r if op == "in" else not r
        if a[0] == "v" and b[0] == "v":
            num = ("int", "float", "bool")
            if (a[1] in num and b[1] in num) or (a[1] == b[1] and a[1] in ("str", "bytes")):
                x, y = a[2], b[2]
                return {"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[op]
        raise Unknown

    # ---- the new operand of an edited comparison

    def literal(self, node):
        if isinstance(node, ast.Constant):
            v = node.value
            if type(v) in (type(None), bool, int, float, str, bytes):
                return ("v", type(v).__name__, v)
            raise Unknown
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant) \
                and type(node.operand.value) in (int, float):
            v = -node.operand.value
            return ("v", type(v).__name__, v)
        raise Unknown

    def new_operand(self, old, new, recorded, depth=0):
        """Rep of the new operand, given the old operand's AST and its recorded reps."""
        try:
            return self.literal(new)
        except Unknown:
            pass
        if isinstance(new, ast.Attribute) and isinstance(old, ast.Attribute) \
                and ast.dump(new.value) == ast.dump(old.value):
            classes = {r[1] for r in recorded}
            if len(classes) == 1 and all(r[0] == "e" and r[2] == old.attr for r in recorded):
                cls = classes.pop()
                info = self.enums.get(cls)
                if info and new.attr in info.get("canonical", ()):
                    return ("e", cls, new.attr)
            raise Unknown
        if depth == 0 and isinstance(new, (ast.Tuple, ast.List, ast.Set)) and type(new) is type(old):
            kind = {ast.Tuple: "t", ast.List: "l", ast.Set: "s"}[type(new)]
            # elements map to the old display's elements by position when unchanged
            old_items = {}
            for i, e in enumerate(old.elts):
                reps = {r[1][i] for r in recorded if r[0] == kind and len(r[1]) == len(old.elts)}
                if len(reps) == 1 and kind != "s":
                    old_items[ast.dump(e)] = reps.pop()
            items = []
            for e in new.elts:
                if ast.dump(e) in old_items:
                    items.append(old_items[ast.dump(e)])
                    continue
                try:
                    items.append(self.literal(e))
                    continue
                except Unknown:
                    pass
                if isinstance(e, ast.Attribute):
                    # an enum member next to members of the same enum in the old display
                    base = ast.dump(e.value)
                    peers = {old_items.get(ast.dump(o)) for o in old.elts
                             if isinstance(o, ast.Attribute) and ast.dump(o.value) == base}
                    peers.discard(None)
                    classes = {p[1] for p in peers if p[0] == "e"}
                    if len(classes) == 1 and len(peers) == len([p for p in peers if p[0] == "e"]):
                        cls = classes.pop()
                        if e.attr in self.enums.get(cls, {}).get("canonical", ()):
                            items.append(("e", cls, e.attr))
                            continue
                raise Unknown
            if kind == "s":
                items.sort(key=repr)
            return (kind, tuple(items))
        raise Unknown

    def keep_for(self, rel, old, new, everyone):
        """Tests that may see a different result when comparison `old` becomes `new`."""
        if len(old.ops) != 1 or len(new.ops) != 1:
            raise Unknown
        site = self.site(rel, old.lineno, old.col_offset)
        if site is None:
            return everyone  # never evaluated: keep whoever ran the line
        if site["import"]:
            raise Unknown
        old_op = site["op"]
        new_op = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
                  ast.Is: "is", ast.IsNot: "is not", ast.In: "in", ast.NotIn: "not in"}.get(type(new.ops[0]))
        if new_op is None:
            raise Unknown
        sides_old = (old.left, old.comparators[0])
        sides_new = (new.left, new.comparators[0])
        unchanged = [ast.dump(o) == ast.dump(n) for o, n in zip(sides_old, sides_new)]
        replaced = [None, None]
        for i in (0, 1):
            if not unchanged[i]:
                replaced[i] = self.new_operand(sides_old[i], sides_new[i], {s[0][i] for s in site["samples"]})
        keep = site["over"]
        observed = site["over"]
        for (a, b, same), m in site["samples"]:
            observed |= m
            na = a if unchanged[0] else replaced[0]
            nb = b if unchanged[1] else replaced[1]
            try:
                before = self.evaluate(old_op, a, b, same)
                after = self.evaluate(new_op, na, nb, same if all(unchanged) else None)
                if before != after:
                    keep |= m
            except Unknown:
                keep |= m
        return keep | (everyone & ~observed)


def compare_pairs(a, b, out):
    """Collect the (old, new) comparisons that differ; raise Unknown on any other difference."""
    if type(a) is not type(b):
        raise Unknown
    if isinstance(a, ast.Compare):
        if ast.dump(a) != ast.dump(b):
            out.append((a, b))
        return
    if isinstance(a, ast.AST):
        for field in a._fields:
            compare_pairs(getattr(a, field, None), getattr(b, field, None), out)
        return
    if isinstance(a, list):
        if len(a) != len(b):
            raise Unknown
        for x, y in zip(a, b):
            compare_pairs(x, y, out)
        return
    if a != b:
        raise Unknown


def stmt_header(node):
    """The part of a statement its own lines hold."""
    if isinstance(node, (ast.If, ast.While)):
        return node.test
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Return, ast.Expr, ast.Assert,
                         ast.Raise, ast.Delete)):
        return node
    raise Unknown


# --------------------------------------------------------------------------- selection


class Selector:
    def __init__(self, doc, all_mask):
        self.doc = doc
        self.ALL = all_mask
        self.masks = doc["masks"]
        self.lines = doc["lines"]
        self.import_lines = {k: set(v) for k, v in doc["import_lines"].items()}
        self.config = set(doc["config_files"])
        self.cache = {}
        self.test_bit = {tid: 1 << i for i, tid in enumerate(doc["tests"])}
        # tests that start a fresh interpreter: import-time changes reach them unseen
        self.subprocess = doc.get("subprocess_tests", 0)
        # every changed span of the current commit: (rel, lo, hi, "function" | "other")
        self.touched = []
        # (rel, lo, hi) of a "function" span -> [cmp site, times touched by that site's swap]
        self.cmp_filters = {}
        self.last_cmp_site = None
        self._class_users_by_file = None
        self.new_idx = None
        self.last_differs = None
        self.no_import_widen = False  # set to see the selection an unchanged import-time state allows
        self.defaults_only = False
        self.import_widened = False
        self.fixture_checks = {}
        # set for a second look at a commit that selected everything: an import edit or a lost
        # name that would select every test is noted in `deferred` instead, to be settled by
        # the import check (only if that module itself then fails to import)
        self.defer_all = False
        self.deferred = set()

    def all_or_defer(self, rel):
        if self.defer_all and rel.startswith("sqlglot/") and rel.endswith(".py"):
            self.deferred.add(rel)
            return 0
        return self.ALL

    def index(self, rel):
        if rel not in self.cache:
            path = SRC / rel
            text = path.read_bytes().decode("utf-8", "surrogateescape")
            self.cache[rel] = (text, SourceIndex(text))
        return self.cache[rel]

    def line_union(self, rel, lo, hi):
        per = self.lines.get(rel)
        m = 0
        if per:
            for ln in range(lo, hi + 1):
                j = per.get(ln)
                if j is not None:
                    m |= self.masks[j]
        return m

    def touches_import(self, rel, lo, hi):
        imp = self.import_lines.get(rel)
        return bool(imp) and any(ln in imp for ln in range(lo, hi + 1))

    def class_fallback(self, rel, qual, idx):
        doc = self.doc
        m = doc["class_users"].get((rel, qual), 0)
        m |= doc["expr_family_users"].get(qual.split(".")[-1], 0)
        span = idx.class_span(qual)
        if span:
            m |= self.line_union(rel, span[0], span[1])
        if not m:
            m = doc["module_any"].get(rel, 0)
        return m | self.subprocess

    def module_fallback(self, rel, idx, node=None):
        doc = self.doc
        if node is not None:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                return self.all_or_defer(rel)
            names = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    names.add(sub.id)
            if isinstance(node, DEFS + (ast.ClassDef,)):
                names.add(node.name)
            if names & set(doc["exported"].get(rel, ())):
                return self.all_or_defer(rel)
        m = doc["module_any"].get(rel, 0)
        for qual in idx.classes():
            m |= doc["class_users"].get((rel, qual), 0)
        return m | self.subprocess

    def mask_for_old_line(self, rel, idx, line, blk=None):
        st, span = idx.stmt_at(line)
        if st is None:
            self.touched.append((rel, line, line, "other"))
            return self.module_fallback(rel, idx)
        lo, hi = span
        if st["scope"] == "function":
            cm = None
            self.last_differs = None
            self.last_cmp_site = None
            if blk is not None:
                cm = self.compare_mask(rel, st, line, blk, span)
                if cm is None:
                    cm = self.boolop_mask(rel, st, line, blk, span)
            # the weak-mutation run evaluated this site both ways and they never disagreed,
            # in a test or outside one, so no execution, import-time included, can change
            # (a disagreement inside a test could have been a lazy import's, so only none counts)
            same_at_import = cm is not None and self.last_differs == 0
            keep = None
            if cm is None and blk is not None:
                # tests whose recorded comparison results can change (any operator, literals)
                keep = self.operand_compare_mask(rel, st, blk)
            # a statement over several lines: only the lines whose bytecode changed
            changed = self.bytecode_lines(rel, st, blk) if blk is not None and hi > lo else None
            if cm is None and changed is not None:
                cm = 0
                for ln in changed:
                    cm |= self.line_union(rel, ln, ln)
            m = self.line_union(rel, lo, hi) if cm is None else cm
            if keep is not None:
                m &= keep
            m |= self.first_use_mask(rel, idx, lo, hi)
            # import-time effects: only where a changed instruction ran while modules were
            # imported (the body of a lambda that an import-time call returns never runs then;
            # an instruction that ran fired its line's event, so its line is in the map)
            spans = [(ln, ln) for ln in sorted(changed)] if changed is not None else [(lo, hi)]
            if not same_at_import and any(self.touches_import(rel, a, b) for a, b in spans):
                self.import_widened = True
            if not same_at_import and not self.no_import_widen and any(self.touches_import(rel, a, b) for a, b in spans):
                func = st["func"]
                m |= self.broad_for_function(rel, idx, func)
                for a, b in spans:
                    m |= self.import_users_mask(rel, a, b)
                self.touched.append((rel, lo, hi, "other"))
            else:
                self.touched.append((rel, lo, hi, "function"))
                self.note_cmp_filter(rel, lo, hi, cm is not None and self.last_cmp_site or None)
            return m
        if st["kind"] == "stmt" and st["scope"] in ("module", "class") and blk is not None:
            lam = self.lambda_body_mask(rel, st, line, blk)
            if lam is not None:
                mask, spans = lam
                self.touched.extend((rel, a, b, "lambda") for a, b in spans)
                if st["scope"] == "class":
                    return self.class_change(rel, idx, st, line, blk) & mask
                return mask
        if rel == ENV_FILE and st["kind"] == "stmt" and st["scope"] == "module" and blk is not None:
            entry = self.env_entry_mask(rel, idx, st, line, blk)
            if entry is not None:
                return entry
        self.touched.append((rel, lo, hi, "other"))
        if st["kind"] == "def":
            body_lo, body_hi = st["full"]
            node = st["node"]
            # whether the body ran while modules were imported; a stub body (`...`, `pass`, a
            # docstring), such as an overload's, does nothing even when called
            stub = all(isinstance(b, ast.Pass) or (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))
                       for b in node.body)
            at_import = bool(node.body) and not stub and self.touches_import(rel, node.body[0].lineno, body_hi)
            if at_import:
                self.import_widened = True
                at_import = not self.no_import_widen
            narrowed = None
            self.defaults_only = False
            if blk is not None and not at_import:
                # only calls that leave a changed default out can see it
                narrowed = self.default_args_mask(rel, node, blk)
            m = self.line_union(rel, body_lo, body_hi) if narrowed is None else narrowed
            in_decorators = line < st["node"].lineno
            if self.defaults_only and not at_import and not in_decorators and node.body:
                # a literal default only reaches the body of a call that left it out: for the
                # TPC-DS replay this is a change to the body's lines
                self.touched[-1] = (rel, node.body[0].lineno, body_hi, "function")
            if in_decorators and st["scope"] == "class":
                m |= self.class_fallback(rel, st["cls"], idx)
            elif in_decorators:
                m |= self.module_fallback(rel, idx)
            if at_import:
                # the function also runs while modules are imported (a default it now takes
                # differently, a parameter renamed): what those imports built may change
                m |= self.broad_for_function(rel, idx, node)
                m |= self.import_users_mask(rel, node.body[0].lineno, body_hi)
            return m
        if st["scope"] == "class":
            return self.class_change(rel, idx, st, line, blk)
        if blk is not None and isinstance(st["node"], (ast.Assign, ast.AnnAssign)):
            entry = self.dict_entry_mask(rel, st["node"], line, blk)
            if entry is not None:
                return entry | self.subprocess
            entry = self.module_value_mask(rel, st["node"], blk)
            if entry is not None:
                return entry | self.subprocess
            entry = self.operand_dict_mask(rel, st["node"], line, blk)
            if entry is not None:
                return entry | self.subprocess
        return self.module_fallback(rel, idx, st["node"])

    # ---- an edit to entries of the executor's ENV table

    def env_entry_mask(self, rel, idx, st, line, blk):
        """A line-for-line edit of sqlglot.executor.env.ENV that changes only some entries
        (a key, or a value: `null_if_any(ord)`, a lambda, `PYTHON_VERSION >= (3, 8)`). ENV is
        read only by the executor, which copies it into the globals its generated code runs
        with, so only lookups of the edited keys can see the change: the tests that read those
        keys (every executor test, through the copy) or the whole dict, from the wrapped-literal
        run. Unless the new value raises while the module is imported, which would break every
        importer: the commit then keeps its usual (module-wide) selection, until a check child
        has imported the patched module (`guard_mask`). None if not provable."""
        if not blk["removed"] or len(blk["removed"]) != len(blk["added"]) or self.new_idx is None:
            return None
        node = st["node"]
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "ENV" and isinstance(node.value, ast.Dict)):
            return None
        new_line = blk["added"][blk["removed"].index(line)]
        new_st, _ = self.new_idx.stmt_at(new_line)
        new = new_st["node"] if new_st is not None and new_st["scope"] == "module" else None
        if not (isinstance(new, ast.Assign) and len(new.targets) == 1 and isinstance(new.targets[0], ast.Name)
                and new.targets[0].id == "ENV" and isinstance(new.value, ast.Dict)):
            return None
        a, b = node.value, new.value
        if len(a.keys) != len(b.keys):
            return None
        keys = set()
        for ka, va, kb, vb in zip(a.keys, a.values, b.keys, b.values):
            if ka is None or kb is None:
                return None
            if ast.dump(ka) == ast.dump(kb) and ast.dump(va) == ast.dump(vb):
                continue
            for k in (ka, kb):
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                    return None
                keys.add(k.value)
        if not keys:
            return None
        ops = load_dictreads()
        d = ops.dict_at(rel, a.lineno, a.col_offset) if ops is not None else None
        if d is None or d["import_bulk"] or any(r == ("?",) or (r[0] == "v" and r[2] in keys) for r in d["import_keys"]):
            return None
        m = d["bulk"]
        for r, mask in d["reads"].items():
            if r == ("?",) or (len(r) == 3 and r[0] == "v" and r[2] in keys):
                m |= mask
        self.guard_mask |= self.module_fallback(rel, idx, node)
        self.env_keys |= keys
        self.touched.append((rel, node.lineno, node.end_lineno or node.lineno, "env"))
        return m | self.subprocess

    # ---- an edit inside the body of a module- or class-level lambda

    @staticmethod
    def _lambda_shape(node):
        """The statement's dump with every outermost lambda's body blanked out, and those lambdas."""
        import copy

        lams = []

        class Blank(ast.NodeTransformer):
            def visit_Lambda(self, n):
                lams.append(n)
                return ast.Lambda(args=n.args, body=ast.Constant(value=None))

        shape = ast.dump(Blank().visit(copy.deepcopy(node)))
        return shape, lams

    def lambda_body_mask(self, rel, st, line, blk):
        """A line-for-line edit of a module- or class-level statement (an ENV entry, a
        TRANSFORMS lambda) that changes nothing but the bodies of lambdas in it: the statement
        builds the same objects at import, and the new code runs only where the lambda is
        called, which is where its lines run in a test's coverage (they also run at import,
        where the lambda is created, outside every test). Only for lambdas nothing calls
        before the tests (tools/build_import_lambdas.py: none while modules import, while
        every dialect is instantiated, or in test_execute_tpcds's table setup). Returns the
        tests that ran those lambdas and their (first, last) line spans, or None."""
        if not blk["removed"] or len(blk["removed"]) != len(blk["added"]) or line not in blk["removed"]:
            return None
        data = load_import_lambdas()
        if data is None or self.new_idx is None:
            return None
        new_line = blk["added"][blk["removed"].index(line)]
        new_st, _ = self.new_idx.stmt_at(new_line)
        old, new = st["node"], new_st["node"] if new_st is not None else None
        if new is None or new_st["kind"] != "stmt" or new_st["scope"] != st["scope"] or type(new) is not type(old):
            return None
        shift = new_line - line
        if new.lineno - old.lineno != shift or (new.end_lineno or 0) - (old.end_lineno or 0) != shift:
            return None
        old_shape, old_lams = self._lambda_shape(old)
        new_shape, new_lams = self._lambda_shape(new)
        if old_shape != new_shape or len(old_lams) != len(new_lams):
            return None
        changed = [a for a, b in zip(old_lams, new_lams) if ast.dump(a.body) != ast.dump(b.body)]
        if not changed:
            return None
        called = data.get(rel, ())
        spans = []
        m = 0
        for lam in changed:
            lo, hi = lam.lineno, lam.end_lineno or lam.lineno
            if any(ln in called for ln in range(lo, hi + 1)):
                return None  # this lambda (or one sharing its lines) is called before tests
            spans.append((lo, hi))
            m |= self.line_union(rel, lo, hi)
        return m | self.subprocess, spans

    # ---- a comparison operator swapped for another

    def compare_mask(self, rel, st, line, blk, span):
        """A one-line edit that only swaps the operator of a single comparison `a OP b`:
        the tests where that swap changes the comparison's result, from the weak-mutation
        run at the pinned commit (tools/build_cmp.py), for the neighbouring operator that
        run computed (is -> ==, is not -> !=, < -> <=, <= -> <, > -> >=, >= -> >): only the
        tests where the two disagreed. None for any other edit, or if they also disagreed
        outside tests."""
        if len(blk["removed"]) != 1 or len(blk["added"]) != 1:
            return None
        old_line, new_line = blk["removed_text"][0], blk["added_text"][0]
        lo, hi = changed_range(old_line, new_line)
        target = None
        for node in ast.walk(st["node"]):
            if isinstance(node, ast.Compare) and len(node.ops) == 1:
                left, right = node.left, node.comparators[0]
                if left.end_lineno == line == right.lineno and left.end_col_offset <= lo and hi <= right.col_offset:
                    target = node
                    break
        if target is None or type(target.ops[0]) not in CMP_NAMES:
            return None
        new_st, _ = self.new_idx.stmt_at(blk["added"][0])
        if new_st is None:
            return None
        new_cmp = None
        for node in ast.walk(new_st["node"]):
            if isinstance(node, ast.Compare) and node.lineno == target.lineno and node.col_offset == target.col_offset:
                new_cmp = node
                break
        if new_cmp is None or len(new_cmp.ops) != 1 or type(new_cmp.ops[0]) not in CMP_NAMES:
            return None
        if ast.dump(new_cmp.left) != ast.dump(target.left) or \
                ast.dump(new_cmp.comparators[0]) != ast.dump(target.comparators[0]):
            return None
        old_op, new_op = CMP_NAMES[type(target.ops[0])], CMP_NAMES[type(new_cmp.ops[0])]
        if old_op == new_op:
            return None
        data = load_cmp()
        if data is None:
            return None
        key = (rel, target.lineno, target.col_offset)
        if key in data["outside"] or data["ops"].get(key) != old_op:
            return None  # also when the recorded run didn't rewrite this comparison
        if CMP_ALT.get(old_op) != new_op:
            return None  # only the neighbouring operator was computed; any other swap flips results
        m = data["differs"].get(key, 0)
        self.last_differs = m
        self.last_cmp_site = key
        # tests the instrumented run couldn't see (fresh interpreters) or that failed there
        return m | (self.line_union(rel, span[0], span[1]) & (self.subprocess | data["failed"]))

    def boolop_mask(self, rel, st, line, blk, span):
        """A one-line edit that only swaps `and` for `or` (or back) in a two-operand boolean
        used as a condition: the tests where the swap changes its truth value, from the
        recorded run (tools/cmp_plugin.py rewrites only such sites, with a side-effect-free
        second operand). None for anything else."""
        if len(blk["removed"]) != 1 or len(blk["added"]) != 1:
            return None
        old_line, new_line = blk["removed_text"][0], blk["added_text"][0]
        lo, hi = changed_range(old_line, new_line)
        target = None
        for node in ast.walk(st["node"]):
            if isinstance(node, ast.BoolOp) and len(node.values) == 2:
                a, b = node.values
                if (a.end_lineno, a.end_col_offset) <= (line, lo) and (line, hi) <= (b.lineno, b.col_offset):
                    target = node
                    break
        if target is None:
            return None
        new_st, _ = self.new_idx.stmt_at(blk["added"][0])
        if new_st is None:
            return None
        new_node = None
        for node in ast.walk(new_st["node"]):
            if isinstance(node, ast.BoolOp) and node.lineno == target.lineno and node.col_offset == target.col_offset:
                new_node = node
                break
        if new_node is None or len(new_node.values) != 2 or type(new_node.op) is type(target.op):
            return None
        if any(ast.dump(x) != ast.dump(y) for x, y in zip(new_node.values, target.values)):
            return None
        data = load_cmp()
        if data is None:
            return None
        key = (rel, target.lineno, target.col_offset)
        old_op = "and" if isinstance(target.op, ast.And) else "or"
        if key in data["outside"] or data["ops"].get(key) != old_op:
            return None
        m = data["differs"].get(key, 0)
        self.last_differs = m
        self.last_cmp_site = key
        return m | (self.line_union(rel, span[0], span[1]) & (self.subprocess | data["failed"]))

    # ---- module-level constants

    PURE_CALLS = {"set", "frozenset", "dict", "tuple", "list", "sorted", "len", "range", "str", "int",
                  "float", "bool", "chr", "ord"}
    PURE_METHODS = {"lower", "upper", "strip", "fromkeys", "items", "keys", "values", "format", "join", "split"}

    @classmethod
    def pure_value(cls, node):
        """True if evaluating this expression at import time can't do anything but build a value."""
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr, ast.Lambda)):
                return False
            if isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Name) and f.id in cls.PURE_CALLS:
                    continue
                if isinstance(f, ast.Attribute) and f.attr in cls.PURE_METHODS:
                    continue
                return False
        return True

    def module_value_mask(self, rel, node, blk):
        """A line-for-line edit inside the value of a plain module-level assignment reaches
        only code that reads the name: the tests that ran those reads (per the reference
        index built from the pinned commit), or that called a function taking it as a
        default argument. None unless that is provable: every read must run after import
        and be visible to coverage, and both old and new values must be side-effect free."""
        if len(blk["removed"]) != len(blk["added"]) or not blk["removed"]:
            return None
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if value is None or not targets or not all(isinstance(t, ast.Name) for t in targets):
            return None
        start, end = (value.lineno, value.col_offset), (value.end_lineno, value.end_col_offset)
        for ln, old_line, new_line in zip(blk["removed"], blk["removed_text"], blk["added_text"]):
            lo, hi = changed_range(old_line, new_line)
            if not (start <= (ln, lo) and (ln, hi) <= end):
                return None  # the edit reaches past the value (a target, another statement)
        new_st, _ = self.new_idx.stmt_at(blk["added"][0])
        if new_st is None or new_st["scope"] != "module" or not isinstance(new_st["node"], type(node)):
            return None
        new_node = new_st["node"]
        new_targets = new_node.targets if isinstance(new_node, ast.Assign) else [new_node.target]
        if [getattr(t, "id", None) for t in new_targets] != [t.id for t in targets] or new_node.value is None:
            return None
        if not (self.pure_value(value) and self.pure_value(new_node.value)):
            return None
        index = load_globals()
        if index is None:
            return None
        m = 0
        for t in targets:
            entry = index["names"].get(t.id)
            if entry is None or entry["unsafe"] or rel not in index["defined"].get(t.id, ()):
                return None
            for f, lo, hi in entry["runtime"] + entry["defaults"]:
                imp = self.import_lines.get(f)
                if imp and any(ln in imp for ln in range(lo, hi + 1)):
                    return None  # this read also runs at import time, before any test
                m |= self.line_union(f, lo, hi)
            for f in entry["test_files"]:
                tm = self.doc["test_files"].get(f)
                if tm is None:
                    return None
                m |= tm
        return m

    # ---- class attribute changes

    def class_change(self, rel, idx, st, line, blk):
        """A class-body statement changed. Dialect classes reach the tests that used the
        dialect (or a subclass), expression classes the tests that built that type; inside
        that set we narrow to tests that could observe the edit, when that is provable."""
        doc = self.doc
        qual = st["cls"]
        users = doc["class_users"].get((rel, qual))
        expr = doc["expr_family_users"].get(qual.split(".")[-1]) if users is None else None
        span = idx.class_span(qual)
        own = self.line_union(rel, span[0], span[1]) if span else 0
        narrowed = self.narrow(rel, idx, st, line, blk) if blk else None
        if users is not None or expr is not None:
            base = (users or 0) | (expr or 0) | own
            m = base if narrowed is None else narrowed & base
        else:
            base = own | doc["module_any"].get(rel, 0)
            m = base if narrowed is None else narrowed
        return m | self.subprocess

    def dict_entry_mask(self, rel, node, line, blk):
        """For a one-line edit inside the value of a dict entry: the tests that ran it if
        the value is a lambda, or the tests that built an X if the key is exp.X.
        None if the edit isn't provably confined to one such entry."""
        if len(blk["removed"]) != 1 or len(blk["added"]) != 1:
            return None
        lo, hi = changed_range(blk["removed_text"][0], blk["added_text"][0])
        here = (line, lo)
        there = (line, hi)
        for d in ast.walk(node):
            if not isinstance(d, ast.Dict):
                continue
            for k, v in zip(d.keys, d.values):
                if not ((v.lineno, v.col_offset) <= here and there <= (v.end_lineno, v.end_col_offset)):
                    continue
                if k is None:
                    return None
                if (k.end_lineno, k.end_col_offset) >= here and (k.lineno, k.col_offset) <= there:
                    return None  # the key itself is touched
                if isinstance(v, ast.Lambda):
                    return self.line_union(rel, v.lineno, v.end_lineno)
                if isinstance(k, ast.Attribute) and isinstance(k.value, ast.Name) and k.value.id == "exp":
                    return self.doc["expr_family_users"].get(k.attr, 0)
                return None
        return None

    def argreq_mask(self, rel, st, node, line, blk):
        """A one-line True/False flip of one entry of an expression class's arg_types. Those
        values only build required_args (at class creation), which only error_messages reads,
        so the flip can only change what it reports for that class, or a class that copies
        its arg_types, when that arg is missing: the tests where that happened in the
        instrumented run at the pinned commit (tools/build_argreq.py). None if unsure."""
        if not rel.startswith("sqlglot/expressions/") or len(blk["removed"]) != 1 or len(blk["added"]) != 1:
            return None
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name) or targets[0].id != "arg_types" \
                or not isinstance(node.value, ast.Dict):
            return None
        lo, hi = changed_range(blk["removed_text"][0], blk["added_text"][0])
        old = self.entry_at(node, line, lo, hi)
        if old is None or not (isinstance(old[0], ast.Constant) and isinstance(old[0].value, str)):
            return None
        if not (isinstance(old[1], ast.Constant) and isinstance(old[1].value, bool)):
            return None
        if (old[1].lineno, old[1].col_offset) > (line, lo):
            return None  # the edit reaches the key
        new_st, _ = self.new_idx.stmt_at(blk["added"][0])
        if new_st is None or not isinstance(new_st["node"], ast.Assign) or not isinstance(new_st["node"].value, ast.Dict):
            return None
        nlo, nhi = changed_range(blk["added_text"][0], blk["removed_text"][0])
        new = self.entry_at(new_st["node"], blk["added"][0], nlo, nhi)
        if new is None or not (isinstance(new[0], ast.Constant) and new[0].value == old[0].value) \
                or not (isinstance(new[1], ast.Constant) and isinstance(new[1].value, bool)):
            return None
        data = load_argreq()
        if data is None:
            return None
        module = rel[:-3].replace("/", ".")
        owners, todo = set(), [f"{module}.{st['cls']}"]
        while todo:
            o = todo.pop()
            if o in owners:
                continue
            owners.add(o)
            short = o.rsplit(".", 1)[-1]
            if short in data["unsafe_names"]:
                return None
            todo.extend(data["derived"].get(short, ()))
        key = old[0].value
        m = data["test_reads"]
        for o in owners:
            if (o, key) in data["outside"]:
                return None
            m |= data["missing"].get((o, key), 0)
        return m

    @staticmethod
    def key_names(k):
        """Canonical names a dict key's source can stand for (as dictkeys_plugin writes
        them), or None if it can't be told from the source."""
        if isinstance(k, ast.Constant):
            if isinstance(k.value, str):
                return {"s:" + k.value}
            if isinstance(k.value, (int, float, bool)) or k.value is None:
                return {"r:" + repr(k.value)}
            return None
        if isinstance(k, ast.Attribute):  # exp.DType.X, TokenType.X, exp.Anonymous
            return {"e:" + k.attr, "c:" + k.attr}
        return None

    @staticmethod
    def entry_at(node, line, lo, hi):
        """(key, value) of the dict entry in `node` holding columns [lo, hi) of `line`."""
        for d in ast.walk(node):
            if not isinstance(d, ast.Dict):
                continue
            for k, v in zip(d.keys, d.values):
                if k is None:
                    continue
                if (k.lineno, k.col_offset) <= (line, lo) and (line, hi) <= (v.end_lineno, v.end_col_offset):
                    return k, v
        return None

    def dict_key_mask(self, rel, node, line, blk, cls=None):
        """A one-line edit to one entry (its key, its value, or both) of a class-level dict
        constant: the tests that looked up the old or the new key in any dict of that name,
        or read such a dict whole from test code (tools/build_dictkeys.py, which also rules
        out names whose dicts are copied, aliased, written or derived from). None if unsure."""
        if len(blk["removed"]) != 1 or len(blk["added"]) != 1:
            return None
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            return None
        attr = targets[0].id
        data = load_dictkeys()
        if data is None or attr not in data["tracked_set"]:
            return None
        # some names are only unsafe for certain keys: those an import-time parse looks up,
        # or that something writes at runtime (tools/build_key_level.py)
        key_level = data.get("key_level", {}).get(attr)
        if attr in data["unsafe"] and key_level is None:
            return None
        old_line, new_line = blk["removed_text"][0], blk["added_text"][0]
        lo, hi = changed_range(old_line, new_line)
        old = self.entry_at(node, line, lo, hi)
        if old is None:
            return None
        names = self.key_names(old[0])
        if names is None:
            return None
        key_touched = (old[0].end_lineno, old[0].end_col_offset) >= (line, lo) and \
            (old[0].lineno, old[0].col_offset) <= (line, hi)
        if key_touched:
            new_st, _ = self.new_idx.stmt_at(blk["added"][0])
            if new_st is None or not isinstance(new_st["node"], (ast.Assign, ast.AnnAssign)):
                return None
            nlo, nhi = changed_range(new_line, old_line)
            new = self.entry_at(new_st["node"], blk["added"][0], nlo, nhi)
            new_names = self.key_names(new[0]) if new is not None else None
            if new_names is None:
                return None
            names = names | new_names
        if key_level is not None and names & key_level:
            return None
        # keys with these characters feed a structure built from the keys at import time
        # (the tokenizer's keyword trie takes keys with a space or a single-token character)
        chars = data.get("key_level_chars", {}).get(attr)
        if chars and any(n.startswith("s:") and any(c in n[2:] for c in chars) for n in names):
            return None
        m = data["full_tests"].get(attr, 0)
        owners = self.dict_owners(data, attr, rel, cls)
        if owners is not None:
            # only the lookups in this class's dict and in the dicts built from it
            owned = data["keys_owned"][attr]
            for o in owners:
                per = owned.get(o, {})
                for k in names:
                    m |= per.get(k, 0)
            return m
        per = data["keys"].get(attr, {})
        for k in names:
            m |= per.get(k, 0)
        return m

    @staticmethod
    def dict_owners(data, attr, rel, cls):
        """The class-level dicts named `attr` that carry an entry of class `cls` in `rel`: its
        own dict and every dict whose class body was built from one of those (merges,
        comprehensions and copies, as dictkeys2_plugin traced them; a dict whose sources
        couldn't be told counts as built from all). None if this dict isn't known."""
        derived = data.get("derived", {}).get(attr)
        if not derived or "keys_owned" not in data or attr not in data["keys_owned"] or not cls:
            return None
        module = rel[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        owner = f"{module}.{cls}"
        if owner not in derived:
            return None
        children = {}
        unknown = [o for o, srcs in derived.items() if srcs is None]
        for o, srcs in derived.items():
            for src in srcs or ():
                children.setdefault(src, set()).add(o)
        out = {owner, *unknown}
        todo = list(out)
        while todo:
            cur = todo.pop()
            for nxt in children.get(cur, ()):
                if nxt not in out:
                    out.add(nxt)
                    todo.append(nxt)
        return out

    def operand_dict_mask(self, rel, node, line, blk):
        """A one-line edit inside the value of one entry of a module/class-level dict literal:
        the tests that read that key, or read the dict in bulk, from the wrapped-literal run
        (tools/operand_plugin.py). Used where our own per-name key index gives up (a dict
        another object keeps a reference to, or one copied at import). None if unsure: the
        key itself touched, a value that isn't plain data, or a dict read at import."""
        if len(blk["removed"]) != 1 or len(blk["added"]) != 1 or not isinstance(node, (ast.Assign, ast.AnnAssign)) \
                or not isinstance(node.value, ast.Dict):
            return None
        lo, hi = changed_range(blk["removed_text"][0], blk["added_text"][0])
        here, there = (line, lo), (line, hi)
        d = node.value
        for k, v in zip(d.keys, d.values):
            if not ((v.lineno, v.col_offset) <= here and there <= (v.end_lineno, v.end_col_offset)):
                continue
            if k is None or ((k.end_lineno, k.end_col_offset) >= here and (k.lineno, k.col_offset) <= there):
                return None
            if v.lineno != line or v.end_lineno != line:
                return None
            # the new value: the edited line's slice that replaced the old value
            new_line = blk["added_text"][0].encode("utf-8", "surrogateescape")
            old_line = blk["removed_text"][0].encode("utf-8", "surrogateescape")
            delta = len(new_line) - len(old_line)
            try:
                new_value = ast.parse(new_line[v.col_offset:v.end_col_offset + delta].decode("utf-8"),
                                      mode="eval").body
            except (SyntaxError, UnicodeDecodeError, ValueError):
                return None
            ops = load_dictreads()
            if ops is None:
                return None
            try:
                m = ops.dict_readers(rel, d, k, v, new_value)
            except Unknown:
                return None
            log(f"dict entry edit in {rel}:{line} narrowed to the tests that read its key (wrapped literal)")
            return m
        return None

    def operand_compare_mask(self, rel, st, blk):
        """For an edit to one function statement that only changes comparisons (an operator
        swap, a changed literal operand): the tests whose recorded comparison results may
        change (see Operands). None if not provable."""
        if not blk["removed"] or not blk["added"] or self.new_idx is None:
            return None
        idx = self.cache[rel][1]
        olds = {id(s): s for s in (idx.stmt_at(ln)[0] for ln in blk["removed"])}
        news = {id(s): s for s in (self.new_idx.stmt_at(ln)[0] for ln in blk["added"])}
        if len(olds) != 1 or len(news) != 1:
            return None
        (st_old,), (st_new,) = olds.values(), news.values()
        if st_old is None or st_new is None or st_old is not st:
            return None
        if st_old["scope"] != "function" or st_new["scope"] != "function" or st_old["kind"] != "stmt" \
                or st_new["kind"] != "stmt":
            return None
        # (a changed comparison that ran while modules were imported makes keep_for give up)
        ops = load_operands()
        if ops is None:
            return None
        try:
            pairs = []
            compare_pairs(stmt_header(st_old["node"]), stmt_header(st_new["node"]), pairs)
            if not pairs:
                return None
            keep = 0
            for old, new in pairs:
                for o, n in ((old.left, new.left), (old.comparators[0], new.comparators[0])):
                    if ast.dump(o) != ast.dump(n) and not all(isinstance(x, self.OPERAND_PURE) for x in ast.walk(o)):
                        raise Unknown  # dropping the old operand's evaluation could matter
                keep |= ops.keep_for(rel, old, new, self.ALL)
        except Unknown:
            return None
        except RecursionError:
            return None
        log(f"comparison edit in {rel}:{st_old['own'][0]} narrowed by recorded operands")
        return keep

    OPERAND_PURE = (ast.Constant, ast.Name, ast.Attribute, ast.Tuple, ast.List, ast.Set, ast.UnaryOp, ast.USub,
                    ast.Load, ast.expr_context)

    LITERAL_TYPES = (bool, int, float, str, bytes, type(None))

    @classmethod
    def literal_default(cls, node):
        if isinstance(node, ast.Constant) and type(node.value) in cls.LITERAL_TYPES:
            return True
        return isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)) \
            and isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float)

    def default_args_mask(self, rel, node, blk):
        """An edit to a def line that only changes literal default values: the tests in which
        some call left one of those parameters at its default (tools/defaults_plugin.py).
        None if unsure: anything else in the signature or the decorators changed, a
        default that isn't a literal, a function the run didn't see, or a default used
        outside tests."""
        if not blk["removed"] or len(blk["removed"]) != len(blk["added"]) or self.new_idx is None:
            return None
        head_lo = min([node.lineno] + [d.lineno for d in node.decorator_list])
        head_hi = node.body[0].lineno - 1 if node.body else node.lineno
        if any(not (head_lo <= ln <= head_hi) for ln in blk["removed"]):
            return None
        new_st, _ = self.new_idx.stmt_at(blk["added"][0])
        if new_st is None or new_st["kind"] != "def" or type(new_st["node"]) is not type(node):
            return None
        new = new_st["node"]
        new_lo = min([new.lineno] + [d.lineno for d in new.decorator_list])
        new_hi = new.body[0].lineno - 1 if new.body else new.lineno
        if any(not (new_lo <= ln <= new_hi) for ln in blk["added"]) or new.name != node.name:
            return None
        a, b = node.args, new.args
        if len(a.defaults) != len(b.defaults) or len(a.kw_defaults) != len(b.kw_defaults):
            return None

        def shape(fn):
            x = fn.args
            args = ast.arguments(posonlyargs=x.posonlyargs, args=x.args, vararg=x.vararg, kwonlyargs=x.kwonlyargs,
                                 kw_defaults=[None if d is None else ast.Constant(0) for d in x.kw_defaults],
                                 kwarg=x.kwarg, defaults=[ast.Constant(0) for _ in x.defaults])
            return (ast.dump(args), [ast.dump(d) for d in fn.decorator_list],
                    ast.dump(fn.returns) if fn.returns is not None else None, fn.name)

        if shape(node) != shape(new):
            return None
        positional = (a.posonlyargs + a.args)[len(a.posonlyargs) + len(a.args) - len(a.defaults):]
        changed = []
        for arg, d_old, d_new in zip(positional, a.defaults, b.defaults):
            if ast.dump(d_old) != ast.dump(d_new):
                changed.append((arg.arg, d_old, d_new))
        for arg, d_old, d_new in zip(a.kwonlyargs, a.kw_defaults, b.kw_defaults):
            if (d_old is None) != (d_new is None):
                return None
            if d_old is not None and ast.dump(d_old) != ast.dump(d_new):
                changed.append((arg.arg, d_old, d_new))
        if not changed or not all(self.literal_default(o) and self.literal_default(n) for _, o, n in changed):
            return None
        self.defaults_only = True  # only what the body sees changes: it counts as function code
        data = load_defaults()
        if data is None:
            return None
        entry = (data.get(rel) or {}).get((head_lo, node.name))
        if entry is None:
            return None
        m = 0
        for name, _, _ in changed:
            if name not in entry["params"] or name in entry["outside"]:
                return None
            m |= entry["used"].get(name, 0)
        log(f"default edit in {rel}:{node.lineno} narrowed to the calls that left it out")
        # tests in a fresh interpreter weren't seen by the recording run
        return m | (self.line_union(rel, node.lineno, node.end_lineno) & self.subprocess)

    def owner_reads_mask(self, rel, st, node, blk):
        """A one-line edit to the literal value of an UPPER_CASE class constant: the tests whose
        reads of it resolved to this class (tools/attrs_plugin.py), for names no import-time
        code reads. None if unsure."""
        if len(blk["removed"]) != 1 or len(blk["added"]) != 1 or self.new_idx is None:
            return None
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name) or node.value is None:
            return None
        name = targets[0].id
        if not name.isupper() or name.startswith("_"):
            return None  # not a wrapped constant
        read_at_import = name not in self.doc["attr_read_masks"]
        new_st, _ = self.new_idx.stmt_at(blk["added"][0])
        if new_st is None or not isinstance(new_st["node"], type(node)) or new_st["cls"] != st["cls"]:
            return None
        new = new_st["node"]
        new_targets = new.targets if isinstance(new, ast.Assign) else [new.target]
        if [getattr(t, "id", None) for t in new_targets] != [name] or new.value is None:
            return None
        if not (self.literal_default(node.value) and self.literal_default(new.value)):
            return None
        if isinstance(node, ast.AnnAssign) and ast.dump(node.annotation) != ast.dump(new.annotation):
            return None
        data = load_attr_owners()
        if data is None:
            return None
        entry = (data.get(rel) or {}).get((st["cls"], name))
        if entry is None or entry["outside"]:
            return None
        if read_at_import:
            # some read of the name runs at import: only if the import-time state check finds
            # nothing changed but this binding (what those reads built is in that state)
            self.import_widened = True
            if not self.no_import_widen:
                return None
            module = rel[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[: -len(".__init__")]
            self.state_masks.append((module, st["cls"], name))
        log(f"class constant edit in {rel}:{blk['removed'][0]} narrowed to the reads of {st['cls']}.{name}")
        return entry["mask"]

    @staticmethod
    def plain_data_class(cls_node):
        """Enums, dataclasses and named tuples use class attributes without attribute reads."""
        if cls_node is None:
            return True
        for base in cls_node.bases:
            text = ast.unparse(base)
            if text.endswith(("Enum", "Flag", "AutoName", "NamedTuple", "TypedDict")):
                return True
        for dec in cls_node.decorator_list:
            if "dataclass" in ast.unparse(dec):
                return True
        return False

    def narrow(self, rel, idx, st, line, blk):
        """Tests that can observe a one-line edit to a class attribute, or None if unsure:
        a dict-entry narrowing, intersected with the tests that ran code reading the
        attribute (names read at import time are never narrowed)."""
        node = st["node"]
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            return None
        if self.plain_data_class(idx.class_node(st["cls"])):
            return None
        mask = self.dict_entry_mask(rel, node, line, blk)
        if mask is None:
            mask = self.dict_key_mask(rel, node, line, blk, st.get("cls"))
        if mask is None:
            mask = self.argreq_mask(rel, st, node, line, blk)
        if mask is None:
            mask = self.operand_dict_mask(rel, node, line, blk)
        if len(blk["removed"]) != 1 or len(blk["added"]) != 1:
            return mask
        if mask is None:
            owned = self.owner_reads_mask(rel, st, node, blk)
            if owned is not None:
                return owned
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if targets and all(isinstance(t, ast.Name) for t in targets):
            reads = [self.doc["attr_read_masks"].get(t.id) for t in targets]
            if all(r is not None for r in reads):
                rm = 0
                for r in reads:
                    rm |= r
                mask = rm if mask is None else mask & rm
        return mask

    def bytecode_lines(self, rel, st, blk):
        """Old-file lines of the instructions an edit changes inside one function, or None.

        Coverage is per line, and a test fires a line's event whenever it runs any
        instruction compiled to that line. So within a statement spread over several lines
        (a long condition, a call, a conditional expression), only the tests that ran the
        lines holding changed instructions can see the edit: a sub-expression that was
        short-circuited, or a branch not taken, compiled to its own line, never ran. The
        enclosing function is compiled before and after the edit (on its own, the same
        way both times) and the instruction streams compared, nested code included; a
        folded constant lands on the line the compiler gives it, so that line is used.
        Only for line-for-line edits inside one function."""
        key = ("bytecode", rel, tuple(blk["removed"]), tuple(blk["added"]))
        if key not in self.cache:
            try:
                self.cache[key] = self._bytecode_lines(rel, st, blk)
            except Exception as error:  # any surprise: keep the whole statement
                log(f"bytecode comparison failed in {rel}: {error!r}")
                self.cache[key] = None
        return self.cache[key]

    def _bytecode_lines(self, rel, st, blk):
        if not blk["removed"] or len(blk["removed"]) != len(blk["added"]):
            return None
        func = st["func"]
        if func is None:
            return None
        delta = blk["added"][0] - blk["removed"][0]
        new_st, _ = self.new_idx.stmt_at(blk["added"][0])
        new_func = new_st["func"] if new_st is not None else None
        if new_func is None or new_func.name != func.name or new_func.lineno != func.lineno + delta:
            return None
        if any(func.lineno > ln or ln > func.end_lineno for ln in blk["removed"]):
            return None
        import dis
        import types

        def code_of(node):
            try:
                mod = compile(ast.Module(body=[node], type_ignores=[]), rel, "exec", dont_inherit=True)
            except (SyntaxError, ValueError, TypeError):
                return None
            for c in mod.co_consts:
                if isinstance(c, types.CodeType) and c.co_name == node.name:
                    return c
            return None

        def stream(co):
            """(key, line, jump target index or None) per instruction, and nested code."""
            ins_list = list(dis.get_instructions(co))
            index_of = {ins.offset: i for i, ins in enumerate(ins_list)}
            out, nested = [], []
            for ins in ins_list:
                arg = ins.argval
                target = None
                if isinstance(arg, types.CodeType):
                    nested.append(arg)
                    shown = "<code>"
                elif ins.opcode in dis.hasjrel or ins.opcode in dis.hasjabs:
                    shown = ""
                    target = index_of.get(arg, -1)  # compared through the alignment below
                else:
                    shown = repr(arg)
                line = ins.positions.lineno if ins.positions is not None else None
                out.append(((ins.opname, shown), line, target))
            return out, nested

        old_co, new_co = code_of(func), code_of(new_func)
        if old_co is None or new_co is None:
            return None
        lines = set()
        todo = [(old_co, new_co)]
        while todo:
            a, b = todo.pop()
            (sa, na), (sb, nb) = stream(a), stream(b)
            if len(na) != len(nb):
                return None
            todo.extend(zip(na, nb))
            ka, kb = [x[0] for x in sa], [x[0] for x in sb]
            to_new = {}
            ops = difflib.SequenceMatcher(None, ka, kb, autojunk=False).get_opcodes()
            for tag, i1, i2, j1, j2 in ops:
                if tag == "equal":
                    for k in range(i2 - i1):
                        to_new[i1 + k] = j1 + k
            changed_a, changed_b = set(), set()
            for tag, i1, i2, j1, j2 in ops:
                if tag != "equal":
                    changed_a.update(range(i1, i2))
                    changed_b.update(range(j1, j2))
            # an aligned jump whose target moved changes control flow where it stands
            for i, j in to_new.items():
                ta, tb = sa[i][2], sb[j][2]
                if ta is None and tb is None:
                    continue
                if ta is None or tb is None or to_new.get(ta) != tb:
                    changed_a.add(i)
                    changed_b.add(j)
            for i in changed_a:
                if sa[i][1] is None:
                    return None
                lines.add(sa[i][1])
            for j in changed_b:
                if sb[j][1] is None:
                    return None
                lines.add(sb[j][1] - delta)
        if not lines or any(func.lineno > ln or ln > func.end_lineno for ln in lines):
            return None
        return lines

    def first_use_mask(self, rel, idx, lo, hi):
        """Code whose result a process keeps after its first use, such as the generator's
        per-class dispatch table: coverage credits it to whichever test happened to run
        first, but every test that reads the kept result depends on it."""
        spec = FIRST_USE.get(rel)
        if spec is None:
            return 0
        builder, cache = spec
        key = ("first_use", rel)
        if key not in self.cache:
            spans, users = [], set()
            for node in ast.walk(idx.tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == builder:
                    spans.append((node.lineno, node.end_lineno))
            for i, text in enumerate(idx.lines, start=1):
                if cache in text and not text.startswith(cache):  # uses inside functions
                    users.add(i)
            spans += [(i, i) for i in users]
            m = 0
            for i in users:
                m |= self.line_union(rel, i, i)
            self.cache[key] = (spans, m)
        spans, m = self.cache[key]
        if any(a <= hi and lo <= b for a, b in spans):
            return m | self.subprocess
        return 0

    def file_users(self, f):
        """Tests that use a module: those that ran its code or used its classes; for a test
        file, its own tests."""
        doc = self.doc
        if f.startswith("tests/"):
            return doc["test_files"].get(f, 0)
        if self._class_users_by_file is None:
            by = {}
            for (r, _), mask in doc["class_users"].items():
                by[r] = by.get(r, 0) | mask
            self._class_users_by_file = by
        return doc["module_any"].get(f, 0) | self._class_users_by_file.get(f, 0)

    @staticmethod
    def module_file(name):
        """The repository path of a module of ours, or None."""
        base = name.replace(".", "/")
        for rel in (base + ".py", base + "/__init__.py"):
            if (SRC / rel).is_file():
                return rel
        return None

    def import_sites(self):
        """Every import statement under sqlglot/ and tests/: (rel, line, end line, nested,
        names of the modules it can import)."""
        if getattr(self, "_import_sites", None) is None:
            sites = []
            for sub in ("sqlglot", "tests"):
                for path in sorted((SRC / sub).rglob("*.py")):
                    rel = path.relative_to(SRC).as_posix()
                    if "/fixtures/" in rel:
                        continue
                    try:
                        tree = ast.parse(path.read_bytes())
                    except (OSError, SyntaxError, ValueError):
                        continue
                    pkg = rel[:-3].replace("/", ".")
                    pkg = pkg[: -len(".__init__")] if pkg.endswith(".__init__") else pkg.rpartition(".")[0]
                    for node in ast.walk(tree):
                        names = set()
                        if isinstance(node, ast.Import):
                            for a in node.names:
                                names.add(a.name)
                        elif isinstance(node, ast.ImportFrom):
                            mod = node.module or ""
                            if node.level:
                                parts = pkg.split(".")
                                up = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                                mod = ".".join(up + ([mod] if mod else []))
                            names.add(mod)
                            for a in node.names:
                                # a submodule, or a class a package's __getattr__ loads lazily
                                # from its lower-case module (sqlglot.dialects.Athena)
                                names.add(f"{mod}.{a.name}")
                                names.add(f"{mod}.{a.name.lower()}")
                        else:
                            continue
                        full = set()
                        for n in names:  # importing a.b.c imports a and a.b first
                            parts = n.split(".")
                            full.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
                        sites.append((rel, node.lineno, node.end_lineno or node.lineno, node.col_offset > 0, full))
            self._import_sites = sites
        return self._import_sites

    def import_failure_mask(self, entries):
        """The tests an import failure can reach, from the import check's report of what fails
        with the patch that doesn't at the pinned commit ("module: Error", "dialect key:
        Error"): every user of a failing module (ran its code, used its dialect classes), the
        tests running an import of it inside a function, every test module importing it, and
        the test modules that fail to import. None if a failure can't be placed."""
        doc = self.doc
        failing, test_mods = set(), set()
        for entry in entries:
            what = entry.rpartition(": ")[0] or entry
            if what == "refresh":
                continue  # the patched module's own re-import: its module is reported as well
            if what.startswith("dialect "):
                key = what[len("dialect "):]
                if key == "registry" or not re.fullmatch(r"\w+", key):
                    return None
                what = f"sqlglot.dialects.{key}"
            top = what.split(".")[0]
            if what == "sqlglot" or top not in ("sqlglot", "tests"):
                return None
            rel = self.module_file(what)
            if rel is None:
                return None
            (test_mods if top == "tests" else failing).add(what)
        if not failing and not test_mods:
            return None
        m = self.subprocess
        for name in failing:
            m |= self.file_users(self.module_file(name))
        hit = failing | test_mods
        for rel, lo, hi, nested, names in self.import_sites():
            if not names & hit:
                continue
            if rel.startswith("tests/"):
                m |= self.test_module_mask(rel)
            elif nested:
                m |= self.line_union(rel, lo, hi)
        for name in test_mods:
            m |= self.test_module_mask(self.module_file(name))
        # tests that name a failing module (or its dialect class) in their own body, say by
        # reading sqlglot.dialects.Snowflake without building one
        leaves = {n.rsplit(".", 1)[-1] for n in failing}
        pat = re.compile(r"(?<![\w])(?:" + "|".join(map(re.escape, sorted(leaves))) + r")(?![\w])", re.I)
        for rel, cls, name, text in self.test_bodies():
            if pat.search(text):
                m |= self.test_bit.get(f"{rel}::{cls}::{name}", 0)
        return m

    def test_bodies(self):
        """(test file, class, method, source) of every method of a test class."""
        if getattr(self, "_test_bodies", None) is None:
            out = []
            for rel in sorted(self.doc["test_files"]):
                try:
                    text = (SRC / rel).read_bytes().decode("utf-8", "surrogateescape")
                    tree = ast.parse(text)
                except (OSError, SyntaxError, ValueError):
                    continue
                lines = text.split("\n")
                for node in tree.body:
                    if isinstance(node, ast.ClassDef):
                        for f in node.body:
                            if isinstance(f, DEFS):
                                out.append((rel, node.name, f.name,
                                            "\n".join(lines[f.lineno - 1: f.end_lineno or f.lineno])))
            self._test_bodies = out
        return self._test_bodies

    def import_users_mask(self, rel, lo, hi):
        """A function body that also runs while other modules are imported (the trie builder
        every tokenizer class calls, the error formatter a dialect's import-time parse
        reaches) shapes those modules, or stops them importing: every test that uses one of
        them, or a module whose own import imports one of them (tools/build_import_users.py)."""
        data = load_imports()
        if data is None:
            return self.ALL  # without the data, any import-time body could matter anywhere
        per = data["cross"].get(rel)
        if not per:
            return 0
        m = 0
        for ln in range(lo, hi + 1):
            v = per.get(ln)
            if v is None:
                continue
            everyone, files, tests = v if len(v) == 3 else (*v, ())
            if everyone:
                return self.ALL
            for f in files:
                m |= self.file_users(f)
            for tid in tests:  # tests that import one of them while they run
                if tid.startswith("FILE:"):
                    m |= self.doc["test_files"].get(tid[5:], 0)
                else:
                    m |= self.test_bit.get(tid, 0)
        return m | self.subprocess if m else 0

    def broad_for_function(self, rel, idx, func):
        """A function whose body also runs at import time: treat like a change where it's defined."""
        for st in idx.stmts:
            if st["node"] is func:
                if st["scope"] == "class":
                    return self.class_fallback(rel, st["cls"], idx)
                return self.module_fallback(rel, idx)
        return self.module_fallback(rel, idx)

    # ---- per file kinds

    def python_source(self, rel, fd):
        old_text, idx = self.index(rel)
        new_text, blocks = apply_hunks(old_text, fd["hunks"])
        self.patched[rel] = new_text
        try:
            new_idx = SourceIndex(new_text)
        except SyntaxError:
            raise Fallback(f"syntax error in {rel}")
        self.new_idx = new_idx
        self.check_coding(rel, blocks, new_text)
        if self.same_code(blocks, idx, new_idx):
            log(f"{rel}: only layout changed (same syntax tree)")
            return 0
        m = 0
        for blk in blocks:
            if self.comment_only(blk, idx, new_idx):
                continue
            for ln in blk["removed"]:
                m |= self.mask_for_old_line(rel, idx, ln, blk)
            if blk["added"]:
                m |= self.mask_for_added(rel, idx, new_idx, blk)
        m |= self.removed_names_mask(rel, idx, new_idx)
        return m

    # ---- edits whose effect isn't where their lines run

    @staticmethod
    def check_coding(rel, blocks, new_text):
        """A coding declaration (line 1 or 2) decides how the whole file is decoded: unless the
        new text decodes the same under it, every test runs."""
        if not any(ln <= 2 for blk in blocks for ln in blk["removed"] + blk["added"]):
            return
        for line in new_text.split("\n")[:2]:
            mt = re.match(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)", line)
            if not mt:
                continue
            raw = new_text.encode("utf-8", "surrogateescape")
            try:
                if raw.decode(mt.group(1)) == raw.decode("utf-8"):
                    return
            except (LookupError, UnicodeError):
                pass
            raise Fallback(f"coding declaration in {rel}")

    @staticmethod
    def same_code(blocks, idx, new_idx):
        """Whether every block only moves whitespace around and the syntax trees match (line
        breaks inside brackets, trailing spaces, blank lines): nothing Python runs changes."""
        if not blocks:
            return False
        for blk in blocks:
            a = re.sub(r"\s+", "", "".join(blk["removed_text"]))
            b = re.sub(r"\s+", "", "".join(blk["added_text"]))
            if a != b:
                return False
        return ast.dump(idx.tree) == ast.dump(new_idx.tree)

    def name_used_elsewhere(self, name, rel):
        """Whether `name` appears as a word in any Python file under sqlglot/ or tests/ other
        than `rel` (the patched text where the commit edits one)."""
        if getattr(self, "_all_texts", None) is None:
            texts = {}
            for sub in ("sqlglot", "tests"):
                for path in sorted((SRC / sub).rglob("*.py")):
                    r = path.relative_to(SRC).as_posix()
                    if "/fixtures/" in r:
                        continue
                    try:
                        texts[r] = path.read_bytes().decode("utf-8", "surrogateescape")
                    except OSError:
                        continue
            self._all_texts = texts
        for r, text in self._all_texts.items():
            if r != rel and has_word(self.patched.get(r, text), name):
                return True
        for r, text in self.patched.items():
            if r != rel and r not in self._all_texts and has_word(text, name):
                return True
        return False

    def removed_names_mask(self, rel, idx, new_idx):
        """A module- or class-level function or class that no longer exists under its name
        (deleted or renamed): anything that looked it up now fails, wherever it did. A name
        another file mentions, or one this file mentions outside a function body (read at
        import), runs every test; otherwise the module's and its classes' users."""
        def defs(index):
            out = {}
            for st in index.stmts:
                if st["kind"] in ("def", "class") and st["scope"] in ("module", "class"):
                    out.setdefault((st["cls"] if st["scope"] == "class" else None, st["node"].name), st)
            return out

        old, new = defs(idx), defs(new_idx)
        gone = [k for k in old if k not in new]
        if not gone:
            return 0
        m = 0
        for cls, name in gone:
            if name.startswith("__") and name.endswith("__") or self.name_used_elsewhere(name, rel):
                log(f"{rel}: {name} removed or renamed and used elsewhere")
                if self.all_or_defer(rel):
                    return self.ALL
                continue
            pat = re.compile(r"(?<![\w])" + re.escape(name) + r"(?![\w])")
            at_import = False
            for ln, line in enumerate(new_idx.lines, start=1):
                if pat.search(line):
                    st, _ = new_idx.stmt_at(ln)
                    if st is None or st["scope"] != "function":
                        at_import = True
                        break
            if at_import:
                if self.all_or_defer(rel):
                    return self.ALL
                continue
            m |= self.module_fallback(rel, idx, old[(cls, name)]["node"])
        return m

    def mask_for_added(self, rel, idx, new_idx, blk):
        """Added lines inside a function run where the lines around them ran. A new or
        rewritten class attribute, method or class changes the class; a new module
        statement changes the module."""
        old_defs = {(st["cls"], st["node"].name) for st in idx.stmts if st["kind"] in ("def", "class")}
        same_shape = len(blk["removed"]) == len(blk["added"])
        only_new_defs = True  # every line is blank, a comment or part of an inert new function
        for ln in blk["added"]:
            if is_comment_or_blank(new_idx.lines[ln - 1]) and not new_idx.in_string(ln):
                continue  # a blank line or comment between statements runs nothing
            st, _ = new_idx.stmt_at(ln)
            if st is not None and self.inert_new_def(rel, new_idx, st, old_defs):
                continue
            if st is not None and st["scope"] == "function":
                outer = self.enclosing_new_def(new_idx, st)
                if outer is not None and self.inert_new_def(rel, new_idx, outer, old_defs):
                    continue
            only_new_defs = False
            if st is None or st["scope"] == "function":
                continue
            if st["kind"] in ("def", "class") and blk["removed"] and (st["cls"], st["node"].name) in old_defs:
                continue  # an existing header was edited; its removed lines already count
            if same_shape and st["kind"] == "stmt":
                continue  # a line-for-line edit of an existing statement: judged in the old file
            self.touched.append((rel, ln, ln, "other"))
            if st["scope"] == "class":
                return self.class_fallback(rel, st["cls"], idx)
            return self.module_fallback(rel, idx, st["node"])
        m = 0
        if only_new_defs and not blk["removed"]:
            log(f"{rel}: only new functions nothing can call")
            return 0
        if not blk["removed"]:
            anchor = blk["anchor"]
            for ln in (anchor - 1, anchor):
                if 0 < ln <= len(idx.lines):
                    m |= self.mask_for_old_line(rel, idx, ln)
        return m

    @staticmethod
    def enclosing_new_def(new_idx, st):
        """The module- or class-level def statement whose body holds `st`."""
        func = st["func"]
        while func is not None:
            for cand in new_idx.stmts:
                if cand["node"] is func:
                    if cand["scope"] in ("module", "class"):
                        return cand
                    func = cand["func"]
                    break
            else:
                return None
        return None

    def inert_new_def(self, rel, new_idx, st, old_defs):
        """A def the commit adds (not a rename: no old def of that scope disappears, checked
        separately) that nothing can call: its name appears in no other line of any source
        under sqlglot/ or tests/, isn't a dunder, isn't a generator handler name (`*_sql`,
        found by dir()), and defining it runs nothing (no decorators but staticmethod,
        classmethod and property, constant defaults, annotations postponed or absent), and
        the module still compiles."""
        if st["kind"] != "def" or st["scope"] not in ("module", "class"):
            return False
        node = st["node"]
        key = (st["cls"], node.name)
        if key in old_defs:
            return False
        cache = self.__dict__.setdefault("_inert_cache", {})
        ck = (rel, id(new_idx), key)
        if ck in cache:
            return cache[ck]
        ok = self._inert_new_def(rel, new_idx, node)
        cache[ck] = ok
        return ok

    def _inert_new_def(self, rel, new_idx, node):
        name = node.name
        if name.startswith("__") or (name.endswith("_sql") and not name.startswith("_")):
            return False
        if any(not (isinstance(d, ast.Name) and d.id in ("staticmethod", "classmethod", "property"))
               for d in node.decorator_list):
            return False
        args = node.args
        if any(not isinstance(d, ast.Constant) for d in args.defaults + [d for d in args.kw_defaults if d is not None]):
            return False
        postponed = any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
                        and any(a.name == "annotations" for a in n.names) for n in new_idx.tree.body)
        every = args.posonlyargs + args.args + args.kwonlyargs + [a for a in (args.vararg, args.kwarg) if a]
        if not postponed and (node.returns is not None or any(a.annotation is not None for a in every)):
            return False
        if getattr(node, "type_params", None):
            return False
        pat = re.compile(r"(?<![\w])" + re.escape(name) + r"(?![\w])")
        hits = [ln for ln, line in enumerate(new_idx.lines, start=1) if pat.search(line)]
        if hits != [node.lineno] or self.name_used_elsewhere(name, rel):
            return False
        try:  # what ast.parse lets through but compiling rejects (a stray nonlocal, break, ...)
            compile(ast.Module(body=[node], type_ignores=[]), rel, "exec", dont_inherit=True)
        except (SyntaxError, ValueError, TypeError):
            return False
        return True

    @staticmethod
    def comment_only(blk, idx, new_idx):
        if not all(is_comment_or_blank(t) for t in blk["removed_text"] + blk["added_text"]):
            return False
        if any(idx.in_string(ln) for ln in blk["removed"]):
            return False
        if any(new_idx.in_string(ln) for ln in blk["added"]):
            return False
        anchor = blk["anchor"]
        if (anchor - 1) in idx.continued:
            return False
        return True

    def test_source(self, rel, fd):
        """A test module's edit: the edited tests, or their classes, and every class deriving
        from an edited class in the test modules that import this one (the dialect tests all
        derive from test_dialect's Validator). A module-level edit, or one that stops the
        module importing, selects its tests and those of every test module importing it
        (or, for a module-level function or class, the importers that name it)."""
        doc = self.doc
        try:
            old_text, idx = self.index(rel)
        except (OSError, SyntaxError):
            self.verifiable = False
            return self.test_module_mask(rel)
        new_text, blocks = apply_hunks(old_text, fd["hunks"])
        self.patched[rel] = new_text
        try:
            new_idx = SourceIndex(new_text)
            compile(new_idx.tree, rel, "exec", dont_inherit=True)
        except (SyntaxError, ValueError, TypeError):
            return self.test_module_mask(rel)
        old_tops = {n.name for n in idx.tree.body if isinstance(n, DEFS + (ast.ClassDef,))}
        new_tops = {n.name for n in new_idx.tree.body if isinstance(n, DEFS + (ast.ClassDef,))}
        if old_tops - new_tops:
            return self.test_module_mask(rel)  # a removed or renamed class or function
        m = 0
        for blk in blocks:
            if self.comment_only(blk, idx, new_idx):
                continue
            for which, lines in (("old", blk["removed"] or [blk["anchor"] - 1, blk["anchor"]]),
                                 ("new", blk["added"])):
                index = idx if which == "old" else new_idx
                for ln in lines:
                    st, _ = index.stmt_at(ln)
                    if st is None:
                        if which == "new" and is_comment_or_blank(index.lines[ln - 1]) and not index.in_string(ln):
                            continue
                        return self.test_module_mask(rel)
                    cls = st["cls"]
                    if not cls:
                        if st["scope"] == "function" or st["kind"] == "def":
                            func = st["func"] if st["scope"] == "function" else st["node"]
                            top = self.top_def(index, func)
                            m |= self.test_module_mask(rel, {top} if top else None)
                            continue
                        if st["kind"] == "class":
                            m |= self.test_class_mask(rel, st["node"].name)
                            continue
                        return self.test_module_mask(rel)
                    top = cls.split(".")[0]
                    func = st["func"] if st["scope"] == "function" else (st["node"] if st["kind"] == "def" else None)
                    method = None
                    if func is not None and func.name.startswith("test") and "." not in cls \
                            and f"{rel}::{cls}::{func.name}" in self.test_bit:
                        # only where the test runs, unless something else calls it by name
                        if len(re.findall(r"(?<![\w])" + re.escape(func.name) + r"(?![\w])", new_text)) <= 1:
                            method = func.name
                    m |= self.test_class_mask(rel, top, method)
        return m

    @staticmethod
    def top_def(index, func):
        """The name of the module-level def that holds `func`."""
        for node in index.tree.body:
            if isinstance(node, DEFS) and node.lineno <= func.lineno <= (node.end_lineno or node.lineno):
                return node.name
        return None

    # ---- files that aren't sqlglot code: config, test support, moved and binary files

    def is_config(self, rel):
        """Files pytest or the test package read before any test: pytest's config files in
        the directories it searches (the top level and the test directories), conftest.py
        anywhere, and the map's list (tests/__init__.py, tests/helpers.py, ...)."""
        base = os.path.basename(rel)
        return rel in self.config or base == "conftest.py" or \
            (base in PYTEST_CONFIGS and os.path.dirname(rel) in ("", "tests", "tests/dialects"))

    def moved_file_mask(self, fd):
        """A binary, deleted or renamed file: the tests that read it, under either name. An
        importable file (Python, extension), a config file or a package marker runs every
        test, except scripts nothing imports and a deleted test module (its tests and those
        of the test modules importing it)."""
        m = 0
        for p in sorted({fd["old"], fd["new"]} - {None}):
            base = os.path.basename(p)
            if self.is_config(p) or (base == "__init__.py" and p.startswith(("sqlglot/", "tests/"))):
                raise Fallback(f"binary, deleted or renamed config file {p}")
            if p.endswith(IMPORTABLE):
                if p.startswith(UNREAD_DIRS):
                    continue
                if fd["deleted"] and not fd["binary"] and p.startswith("tests/") and base.startswith("test_") \
                        and p.endswith(".py"):
                    self.verifiable = False
                    m |= self.test_module_mask(p)
                    continue
                raise Fallback(f"binary, deleted or renamed file {p}")
            opens = self.doc["file_opens"].get(p, 0)
            m |= opens
            self.data_files.add(p)
            self.data_mask |= opens
            self.touched.append((p, 0, 0, "other"))
        return m

    def sibling_readers(self, rel):
        """Tests that read a module in the same package (they may list it)."""
        d = os.path.dirname(rel) + "/"
        m = 0
        for f, mask in self.doc["file_opens"].items():
            if f.startswith(d) and "/" not in f[len(d):]:
                m |= mask
        return m

    def config_mask(self, rel, fd):
        """pyproject.toml, setup.cfg and setup.py at the top level: pytest parses the first two
        when it looks for its configuration (a file it can't parse stops the whole run), and
        nothing in the suite reads them otherwise. An edit selects nothing if pytest still
        parses the file and the settings it could read are unchanged; setup.py only for
        comment edits. tests/helpers.py is test code: the test modules importing it. Every
        other config file, or any doubt, runs every test."""
        if rel == "tests/helpers.py" and fd["hunks"] and not fd["new_file"]:
            return self.test_source(rel, fd)
        if fd["new_file"] or not fd["hunks"] or rel not in ("pyproject.toml", "setup.cfg", "setup.py"):
            raise Fallback(f"config file {rel}")
        try:
            old_text = (ROOT_FILES / rel).read_bytes().decode("utf-8")
        except (OSError, ValueError):
            raise Fallback(f"config file {rel} (no pinned copy)")
        new_text, blocks = apply_hunks(old_text, fd["hunks"])
        try:
            new_text.encode("utf-8")
        except UnicodeError:
            raise Fallback(f"config file {rel} is not UTF-8")
        if rel == "setup.py":
            try:
                idx, new_idx = SourceIndex(old_text), SourceIndex(new_text)
            except SyntaxError:
                raise Fallback("syntax error in setup.py")
            if all(self.comment_only(b, idx, new_idx) for b in blocks):
                return 0
            raise Fallback("setup.py changed")
        if rel == "pyproject.toml":
            import tomllib
            try:
                old, new = tomllib.loads(old_text), tomllib.loads(new_text)

                def settings(d):
                    tool = d.get("tool", {})
                    if not isinstance(tool, dict):
                        return d
                    return {**d, "tool": {k: v for k, v in tool.items() if k not in INERT_TOOLS}}

                same = settings(old) == settings(new)
            except Exception as error:  # noqa: BLE001  pytest can't parse it: nothing runs
                raise Fallback(f"pyproject.toml: {error}")
            if same:
                log("pyproject.toml edit leaves every setting pytest or the build reads unchanged")
                return 0
            raise Fallback("pyproject.toml settings changed")
        sections = ini_sections(new_text)
        if sections is None or "tool:pytest" in sections or "pytest" in sections:
            raise Fallback("setup.cfg: pytest can't parse it or reads it")
        log("setup.cfg edit: pytest still parses it and has no section there")
        return 0

    # ---- test modules: their tests, the classes deriving from an edited class, importers

    def test_texts(self):
        """{rel: text} of every Python file under tests/ (patched where the commit edits it)."""
        if getattr(self, "_test_texts", None) is None:
            texts = {}
            base = SRC / "tests"
            for path in sorted(base.rglob("*.py")):
                rel = path.relative_to(SRC).as_posix()
                if "/fixtures/" in rel:
                    continue
                try:
                    texts[rel] = path.read_bytes().decode("utf-8", "surrogateescape")
                except OSError:
                    continue
            self._test_texts = texts
        out = dict(self._test_texts)
        for rel, text in self.patched.items():
            if rel.startswith("tests/"):
                out[rel] = text
        return out

    def test_importers(self, rels):
        """The test modules that import one of `rels` (paths under tests/), directly or through
        another test module: any file that names the module and imports anything."""
        texts = self.test_texts()
        found, todo = set(rels), list(rels)
        while todo:
            r = todo.pop()
            leaf = r[:-3].rsplit("/", 1)[-1]
            if leaf == "__init__":
                leaf = r.rsplit("/", 2)[-2]
            for other, text in texts.items():
                if other not in found and has_word(text, leaf) and "import" in text:
                    found.add(other)
                    todo.append(other)
        return found

    def test_module_mask(self, rel, names=None):
        """Every test of `rel` and of the test modules importing it. With `names` (module-level
        functions or classes of `rel`), only importers that mention one of them, and their
        own importers."""
        texts = self.test_texts()
        if names is None:
            files = self.test_importers([rel])
        else:
            direct = {rel}
            for other in self.test_importers([rel]) - {rel}:
                text = texts.get(other, "")
                if any(has_word(text, n) for n in names):
                    direct.add(other)
            files = self.test_importers(direct)
        m = 0
        for f in files:
            m |= self.doc["test_files"].get(f, 0)
        return m

    def test_class_mask(self, rel, cls, method=None):
        """The tests of class `cls` of `rel`, and of every class deriving from it in the test
        modules importing it (by base name, transitively). With `method`, a test method that
        nothing else calls: only that test, in each of those classes."""
        texts = self.test_texts()
        files = self.test_importers([rel])
        hier = []
        for f in sorted(files):
            for mt in re.finditer(r"^class\s+(\w+)\s*(?:\(([^)]*)\))?\s*:", texts.get(f, ""), re.M):
                bases = set(re.findall(r"\w+", mt.group(2) or ""))
                hier.append((f, mt.group(1), bases))
        names, grew = {cls}, True
        while grew:
            grew = False
            for f, c, bases in hier:
                if c not in names and bases & names:
                    names.add(c)
                    grew = True
        doc = self.doc
        m = 0
        for f in files:
            for c in names:
                key = f"{f}::{c}"
                if method is not None:
                    m |= self.test_bit.get(f"{key}::{method}", 0)
                else:
                    m |= doc["test_classes"].get(key, 0)
        return m

    def select(self, text):
        doc = self.doc
        m = 0
        self.patched = {}
        self.verifiable = True
        self.data_files = set()  # changed files we don't overlay: their readers can't be checked
        self.state_masks = []  # bindings the import-time state check leaves out (the edited ones)
        self.guard_mask = 0  # the selection to fall back to unless a check child imports the patch
        self.env_keys = set()  # executor ENV keys whose entries changed
        self.import_widened = False
        self.deferred = set()
        self.data_mask = 0
        self.touched = []
        self.cmp_filters = {}
        self.fixture_checks = {}  # test id -> changed cases it alone is selected for
        fixture_m = 0
        diffs = parse_patch(text)
        if not diffs:
            raise Fallback("no file changes found in the patch")
        reader_edit = any((fd["new"] or fd["old"]) in FIXTURE_READER_FILES or fd["old"] in FIXTURE_READER_FILES
                          for fd in diffs)
        for fd in diffs:
            rel = fd["new"] or fd["old"]
            old_rel = fd["old"]
            if rel is None:
                continue
            if fd["mode"] and not fd["hunks"] and not (fd["binary"] or fd["deleted"] or fd["new_file"]) \
                    and (not old_rel or old_rel == rel):
                continue  # only the file mode changed: no test reads it, the contents are the same
            if fd["binary"] or fd["deleted"] or (old_rel and old_rel != rel):
                m |= self.moved_file_mask(fd)
                continue
            narrowed = None
            if rel in FIXTURE_VIEWS and not reader_edit and not fd["new_file"] and fd["hunks"]:
                narrowed = self.fixture_cases(rel, fd)
            if narrowed is None:
                m |= doc["file_opens"].get(rel, 0)  # tests that read this file's text
            else:
                fixture_m |= narrowed  # only the readers of the cases that changed
            if self.is_config(rel):
                m |= self.config_mask(rel, fd)
                continue
            if fd["new_file"] and os.path.basename(rel) == "__init__.py":
                raise Fallback(f"new package {rel}")  # can shadow a module of the same name
            if rel.endswith(".py") and rel.startswith(UNREAD_DIRS):
                continue  # scripts nothing imports (benchmarks, docs)
            if not rel.endswith(".py"):
                # data files and the like: we only overlay Python sources, so a check may only
                # run tests that never open this file (they see exactly the patched sources);
                # a file read outside tests is a config file and runs everything
                self.data_files.add(rel)
                self.data_mask |= doc["file_opens"].get(rel, 0)
            elif not rel.startswith(("sqlglot/", "tests/")):
                self.verifiable = False  # Python we don't overlay
            if fd["new_file"]:
                self.verifiable = False
                if rel.endswith(".py") and rel.startswith("sqlglot/"):
                    # a module beside ones a test lists and reads (pkgutil over sqlglot/typing)
                    m |= self.sibling_readers(rel)
                continue  # a new file nothing imports or opens can't change a test
            if rel.endswith(".py"):
                if not fd["hunks"]:
                    raise Fallback(f"no hunks for {rel}")
                if rel.startswith("sqlglot/"):
                    m |= self.python_source(rel, fd)
                    continue
                if rel.startswith("tests/"):
                    m |= self.test_source(rel, fd)
                else:
                    m |= doc["test_files"].get("tests/test_sqlglotc.py", 0) | doc["test_files"].get("tests/test_build.py", 0)
            self.touched.append((rel, 0, 0, "other"))
        # a case check can only stand in for a test the rest of the commit doesn't select
        self.fixture_checks = {t: c for t, c in self.fixture_checks.items() if not m & self.test_bit.get(t, 0)}
        return m | fixture_m

    # ---- loop tests over SQL fixture cases: select a reader only if a case it reads changed

    _fixture_loader = None
    _fixture_old = {}

    @classmethod
    def fixture_pairs(cls, raw):
        """The cases of a fixture file's bytes, by the tests' own load_sql_fixture_pairs
        (tests/helpers.py at the pinned commit, reading these bytes as open() would)."""
        import io

        if cls._fixture_loader is None:
            source = (SRC / "tests" / "helpers.py").read_text("utf-8")
            space = {"__name__": "br_fixture_helpers", "__file__": str(SRC / "tests" / "helpers.py")}
            exec(compile(source, "tests/helpers.py", "exec"), space)
            cls._fixture_loader = space
        space = cls._fixture_loader
        space["open"] = lambda *args, **kwargs: io.TextIOWrapper(io.BytesIO(raw), encoding=kwargs.get("encoding"))
        try:
            return list(space["load_sql_fixture_pairs"]("x.sql"))
        finally:
            del space["open"]

    def fixture_cases(self, rel, fd):
        """The mask of the file's readers whose cases changed, or None for the file-level rule.
        Records, per "exec" reader, the changed cases a check could clear it on."""
        doc = self.doc
        views = FIXTURE_VIEWS[rel]
        try:
            raw_old = (SRC / rel).read_bytes()
            if rel not in self._fixture_old:
                Selector._fixture_old[rel] = self.fixture_pairs(raw_old)
            old = self._fixture_old[rel]
            new_text, _ = apply_hunks(raw_old.decode("utf-8", "surrogateescape"), fd["hunks"])
            new = self.fixture_pairs(new_text.encode("utf-8", "surrogateescape"))
        except Fallback:
            raise
        except Exception as error:  # the file no longer loads: setUpClass fails, every reader fails
            log(f"{rel}: cases don't load ({error!r}); every reader stays")
            return None
        opens = doc["file_opens"].get(rel, 0)
        if not opens:
            return None

        def view(kind, case):
            if case is None:
                return None
            meta, sql, expected = case
            if kind == "pair":
                return case
            if kind == "sql_expected":
                return sql, expected
            return meta.get("execute"), sql

        n = max(len(old), len(new))
        old_c = old + [None] * (n - len(old))
        new_c = new + [None] * (n - len(new))
        m, mask = 0, opens
        while mask:
            low = mask & -mask
            mask ^= low
            test_id = doc["tests"][low.bit_length() - 1]
            kind = views.get(test_id)
            if kind is None:
                if not test_id.startswith(FIXTURE_LOAD_CLASS):
                    m |= low  # a reader we don't model
                continue  # setUpClass only: the file still loads
            changed = [i for i in range(n) if view(kind, old_c[i]) != view(kind, new_c[i])]
            if not changed:
                continue
            m |= low
            if kind == "exec":
                # cases the test no longer has can't fail it; the rest are re-checked
                cases = [(rel, i, new_c[i][0], new_c[i][1], old_c[i]) for i in changed if new_c[i] is not None]
                if cases:
                    self.fixture_checks[test_id] = cases
        log(f"{rel}: {sum(1 for i in range(n) if old_c[i] != new_c[i])} cases changed")
        return m

    # ---- the 21 s TPC-DS executor test: replay what the edit can reach instead of running it

    def note_cmp_filter(self, rel, lo, hi, site):
        """Remember that this function span was touched by a single operator swap at `site`
        (one the weak-mutation run settles); any other touch of the span cancels it."""
        cur = self.cmp_filters.get((rel, lo, hi))
        if site is None or (cur is not None and cur[0] != site):
            self.cmp_filters[(rel, lo, hi)] = [None, 0]
        elif cur is None:
            self.cmp_filters[(rel, lo, hi)] = [site, 1]
        elif cur[0] is not None:
            cur[1] += 1

    def cmp_query_filter(self, rel, lo, hi, queries):
        """The TPC-DS queries that can see the operator swap on this span, or None to go by
        line coverage. Only for a span every touch of which is one swap at one site, a site
        the per-query run saw differ in no context outside a query, and evaluated by every
        query whose recorded coverage reaches its line (code run once per process is filed
        under the first query in that run, so it gets no narrowing)."""
        cur = self.cmp_filters.get((rel, lo, hi))
        if cur is None or cur[0] is None:
            return None
        if cur[1] != sum(1 for t in self.touched if t == (rel, lo, hi, "function")):
            return None
        data = load_tpcds_cmp()
        if data is None:
            return None
        site = data["sites"].get(cur[0])
        cmp_data = load_cmp()
        if site is None or site["other_differs"] or cmp_data is None or cmp_data["ops"].get(cur[0]) != site["op"]:
            return None
        line = cur[0][1]
        covering = {q["index"] for q in queries
                    if any(line in q[k].get(rel, ()) for k in ("front_sets", "exec_sets", "log_sets"))}
        if not covering <= site["ran"]:
            return None
        return set(site["differs"])

    def tpcds_plan(self, chosen, duration, tpcds_number):
        """A replay plan when it is provably enough and clearly cheaper than the test, else None.

        Only for commits whose every code change is ordinary function code in sqlglot/, the
        body of a lambda nothing calls before the tests, or an executor ENV entry (whose
        queries are those that look its key up), none of which runs in the test's table setup;
        other parts may be data files the test doesn't open or test files it doesn't import. Each query whose recorded coverage reaches a changed line
        is checked by what that line can affect there: if only parsing, optimize() or the
        duckdb transpilation, its optimizer output is compared ("front"); if also the
        debug-log rendering execute() does next, that rendering is checked too ("log"); if
        planning or execution (or the digest code), its rows ("full"). The duckdb
        transpilation, the expected side of the test's comparison, is always compared."""
        if not self.verifiable or not self.patched or not self.touched:
            return None
        # Other parts of a multi-file commit may be data files the test never opens (its own
        # are the TPC fixtures its class setup reads) or test files outside the test's module
        # and what it imports; neither reaches the replay, which uses the recorded tables and
        # queries and the test's own code from the pinned commit.
        tbit = self.test_bit.get(TPCDS_TEST, 0)
        if not tbit or any(self.doc["file_opens"].get(r, 0) & tbit or "/tpc-" in r for r in self.data_files):
            return None
        code = []
        for rel, lo, hi, kind in self.touched:
            if rel in self.data_files or (rel.startswith("tests/") and rel.endswith(".py") and lo == hi == 0
                                          and rel not in TPCDS_TEST_MODULES):
                continue
            if kind not in ("function", "lambda", "env") or not rel.startswith("sqlglot/"):
                return None
            code.append((rel, lo, hi, kind))
        if not code:
            return None
        data = load_tpcds()
        if data is None:
            return None

        def hits(sets, rel, lo, hi):
            lines = sets.get(rel)
            return bool(lines) and any(ln in lines for ln in range(lo, hi + 1))

        env_queries = None
        if any(kind == "env" for *_, kind in code):
            # an ENV entry reaches only the queries whose execution looks its key up (calls
            # it, or names it in generated code: tools/build_tpcds_env.py)
            env_data = load_tpcds_env()
            if env_data is None or not self.env_keys:
                return None
            env_queries = {i for i, u in env_data["queries"].items()
                           if self.env_keys & (set(u["called"]) | set(u["names"]))}
        code = [c for c in code if c[3] != "env"]
        spans = [(rel, lo, hi) for rel, lo, hi, _ in code]
        # a lambda's lines run in the setup when it's created (at import); the lambdas allowed
        # here are never called there (tools/build_import_lambdas.py)
        if any(hits(data["setup_sets"], rel, lo, hi) for rel, lo, hi, kind in code if kind != "lambda"):
            return None
        # Per query, what the changed lines can affect decides how it's checked: "front"
        # (parse, optimize, duckdb transpile: the optimizer output digest and the duckdb SQL),
        # "log" (also the dropped debug-log rendering: it must not raise or alter the
        # expression), or "full" (planning and execution, or the digest code itself: the rows).
        digest_hit = any(hits(data["digest_sets"], *s) for s in spans)
        # a span whose only change is an operator swap the per-query weak-mutation run
        # settles reaches only the queries where that swap gives another result
        only = {s: self.cmp_query_filter(*s, data["queries"]) for s in spans}
        if any(f is not None for f in only.values()):
            log("TPC-DS: operator swap reaches queries " + ", ".join(
                f"{s[0]}:{s[1]} {sorted(f)}" for s, f in only.items() if f is not None))
        modes, reached = {}, []
        for q in data["queries"]:
            live = [s for s in spans if only[s] is None or q["index"] in only[s]]
            in_exec = any(hits(q["exec_sets"], *s) for s in live)
            in_log = any(hits(q["log_sets"], *s) for s in live)
            in_env = env_queries is not None and q["index"] in env_queries
            if not (in_exec or in_log or in_env or any(hits(q["front_sets"], *s) for s in live)):
                continue
            modes[q["index"]] = "full" if in_exec or in_env or digest_hit else ("log" if in_log else "front")
            reached.append(q)
        if not reached and env_queries is None:
            return None  # the test runs this code somewhere the replay doesn't: keep it

        def cost(q):
            mode = modes[q["index"]]
            return q["cpu"] if mode == "full" else q["front_cpu"] + (q.get("log_cpu", 0.01) if mode == "log" else 0.0)

        full = sum(1 for m in modes.values() if m == "full")
        estimate = sum(cost(q) for q in reached) + 1.0
        saving = (pack_cost(chosen, duration) - pack_cost([n for n in chosen if n != tpcds_number], duration)) / 1000
        if estimate > TPCDS_GATE["full" if full else "front"] * saving:
            return None
        reached.sort(key=cost)  # cheapest first: a changed output usually shows early
        # The child stops and keeps the test once it has used this much CPU. It also re-runs
        # to the end the front queries whose optimizer output changed, if that still fits,
        # since a different plan often gives the same rows.
        budget = max(TPCDS_BUDGET * saving, estimate + 1.0)
        mode = "full" if full == len(reached) else "front"
        log(f"TPC-DS replay planned: {len(reached)} queries ({full} full, "
            f"{sum(1 for m in modes.values() if m == 'log')} with the log line), "
            f"est {estimate:.1f}s to save {saving:.1f}s, budget {budget:.1f}s")
        return {"mode": mode, "modes": modes, "queries": [q["index"] for q in reached],
                "estimate": estimate, "budget": budget}

    # ---- test_generator_memory: rerun its per-dialect check only for dialects the edit can reach

    def genmem_plan(self, chosen, duration, genmem_number):
        """Which dialects to recheck, or None to keep the test as is.

        The test builds every dialect's generator 1,100 times and checks that the last 1,000
        builds leave under 1 KB behind. Only sqlglot modules that are loaded when a dialect's
        generator is built can change that dialect's result (tools/build_genmem.py records
        them per dialect, from a fresh interpreter). Only for commits that change nothing but
        sqlglot/ Python files."""
        if not self.verifiable or self.data_files or not self.patched:
            return None
        rels = set(self.patched) | {rel for rel, _, _, _ in self.touched}
        if not rels or any(not (r.startswith("sqlglot/") and r.endswith(".py")) for r in rels):
            return None
        data = load_genmem()
        if data is None:
            return None
        dialects = [d for d in data["order"] if rels & data["loaded_sets"][d]]
        # the test module itself imports a few sqlglot modules; if one changed, the child
        # imports the test module first, since an import-time failure there fails the test
        import_check = bool(rels & data["module_loaded_set"])
        modules = set().union(*(data["loaded_sets"][d] for d in dialects)) if dialects else set()
        if import_check:
            modules |= data["module_loaded_set"]
        estimate = (0.3 + GENMEM_MODULE_S * len(modules) + sum(data["seconds"][d] for d in dialects)) \
            if dialects or import_check else 0.0
        saving = (pack_cost(chosen, duration) - pack_cost([n for n in chosen if n != genmem_number], duration)) / 1000
        if estimate > 0.8 * saving:
            return None
        log(f"generator memory check planned: {len(dialects)} of {len(data['order'])} dialects, "
            f"est {estimate:.1f}s to save {saving:.1f}s")
        return {"dialects": dialects, "import_check": import_check, "estimate": estimate,
                "budget": max(0.85 * saving, estimate + 1.0)}


# --------------------------------------------------------------------------- packing


def pack(numbers, duration):
    if not numbers:
        return []
    items = sorted(numbers, key=lambda n: (-duration[n], n))
    total = sum(duration[n] for n in items)
    top = duration[items[0]]
    best = None
    for k in range(1, min(MAX_WORKERS, len(items)) + 1):
        if best is not None and max(top, math.ceil(total / k)) + WORKER_MS * k >= best[0]:
            continue
        heap = [(0, j) for j in range(k)]
        workers = [[] for _ in range(k)]
        for n in items:
            load, j = heapq.heappop(heap)
            workers[j].append(n)
            heapq.heappush(heap, (load + duration[n], j))
        cost = max(load for load, _ in heap) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, workers)
    return [w for w in best[1] if w]


# --------------------------------------------------------------------------- runtime check
#
# The rules allow running sqlglot's tests while deciding (the CPU time is charged).
# For a selection that is cheap to run but costly to schedule (every worker costs
# 2 s), we run those tests on the patched sources in a forked child and drop the
# ones that pass. Anything we can't run faithfully, or that misbehaves, stays selected.

RUN = os.environ.get("BR_RUN", "1") != "0"
# where pytest looks for its configuration (each it can't parse stops the run)
PYTEST_CONFIGS = {"pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg",
                  "setup.py", ".coveragerc"}
# pyproject.toml tables no test, pytest or install step reads
INERT_TOOLS = {"ruff", "mypy", "black", "isort", "pylint", "codespell", "pyright", "uv"}
# top-level directories of scripts and documents nothing in the suite imports or opens
UNREAD_DIRS = ("benchmarks/", "docs/", "posts/", "pdoc/", ".github/")
IMPORTABLE = (".py", ".pyi", ".pyc", ".so", ".pyd", ".pth")


def has_word(text, word):
    """Whether `word` occurs in `text` not inside a longer identifier."""
    if word not in text:
        return False
    return re.search(r"(?<![\w])" + re.escape(word) + r"(?![\w])", text) is not None


def ini_sections(text):
    """The section names iniconfig (pytest's ini parser) finds in `text`, or None where it
    raises ParseError (same rules as iniconfig 2.x parse_lines/parse_ini_data)."""
    if text.startswith("\ufeff"):
        text = text[1:]
    sections, names, section, last_value = set(), set(), None, False
    for raw in text.splitlines(True):
        line = "" if raw.lstrip()[:1] in ("#", ";") else raw.rstrip()
        if not line:
            continue
        if line[0] == "[":
            head = line
            for c in "#;":
                head = head.split(c)[0].rstrip()
            if head[-1:] == "]":
                name = head[1:-1]
                if not name or name in sections:
                    return None
                sections.add(name)
                section, names, last_value = name, set(), False
                continue
            if not last_value:  # a continuation of nothing
                return None
            continue
        if not line[0].isspace():
            name, sep, _ = line.partition("=")
            if not sep or ":" in name:
                name, sep, _ = line.partition(":")
                if not sep:
                    return None
            name = name.strip()
            if section is None or name in names:
                return None
            names.add(name)
            last_value = True
            continue
        if not last_value:
            return None
    return sections
TIME_FACTOR = float(os.environ.get("BR_TIME_FACTOR", "1.0"))  # our run time / recorded time
COMMIT_OVERHEAD_MS = 300  # fork, importing the patched sqlglot, plus a safety margin
# measured: importing a test module and its dialects; the two heavy files also pay for
# duckdb/pandas and a setUpClass that loads data
FILE_OVERHEAD_MS = {"tests/test_executor.py": 4000, "tests/test_optimizer.py": 2500}
DEFAULT_FILE_OVERHEAD_MS = 150
MAX_TEST_MS = 3000  # longer tests are cheaper to schedule than to run here
SKIP_FILES = {
    "tests/test_sqlglotc.py", "tests/test_build.py", "tests/test_docs.py",
    "tests/test_dialect_imports.py", "tests/test_dialect_entry_points.py",
    "tests/test_integration_loader.py",
}
SKIP_TESTS = set()


def pack_cost(numbers, duration):
    workers = pack(numbers, duration)
    if not workers:
        return 0
    return max(sum(duration[n] for n in w) for w in workers) + WORKER_MS * len(workers)


def plan_check(chosen, duration, info, excluded=()):
    """The cheapest-first prefix of runnable tests whose check is expected to pay off."""
    runnable = sorted((n for n in chosen if info[n]["runnable"] and n not in excluded), key=lambda n: (duration[n], n))
    if not runnable:
        return []
    base = pack_cost(chosen, duration)
    chosen_set = set(chosen)
    best_gain, best_k = 0, 0
    ks = list(range(1, len(runnable) + 1))
    if len(ks) > 120:
        ks = sorted({max(1, int(len(runnable) * f / 60)) for f in range(1, 61)})
    run_ms, files = 0, set()
    prev = 0
    for k in ks:
        for n in runnable[prev:k]:
            run_ms += duration[n] * TIME_FACTOR
            files.add(info[n]["file"])
        prev = k
        overhead = COMMIT_OVERHEAD_MS + sum(FILE_OVERHEAD_MS.get(f, DEFAULT_FILE_OVERHEAD_MS) for f in files)
        rest = sorted(chosen_set - set(runnable[:k]))
        gain = base - pack_cost(rest, duration) - run_ms - overhead
        if gain > best_gain:
            best_gain, best_k = gain, k
    return runnable[:best_k]


OVERLAY_HOOK = r"""
import importlib.abc, importlib.machinery, os, sys
_root, _src = os.environ.get("BR_OVERLAY"), os.environ.get("BR_SRC_ROOT")
if _root and _src:
    _files = os.path.join(_root, "files")
    _by_path = {}
    for _d, _, _names in os.walk(_files):
        for _n in _names:
            _p = os.path.join(_d, _n)
            _by_path[os.path.join(_src, os.path.relpath(_p, _files))] = _p

    class _Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            with open(_by_path[self.path], "rb") as fh:
                return compile(fh.read(), self.path, "exec", dont_inherit=True)

    class _Finder(importlib.abc.MetaPathFinder):
        _br_overlay = True  # marker: virtualenv's own meta path hook is also named _Finder

        def find_spec(self, fullname, path, target=None):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is not None and spec.origin in _by_path:
                spec.loader = _Loader(fullname, spec.origin)
            return spec

    if not any(getattr(f, "_br_overlay", False) for f in sys.meta_path):
        sys.meta_path.insert(0, _Finder())
"""


_TPCDS = None


_GLOBALS = None
_GENMEM = None
_DICTKEYS = None
_CMP = None
_ARGREQ = None
_IMPORTS = None
_OPERANDS = None
_DICTREADS = None
_DEFAULTS = None
_ATTR_OWNERS = None


_IMPORT_LAMBDAS = None
_TPCDS_ENV = None


def load_tpcds_env():
    """Per executed TPC-DS query, the executor ENV keys its execution calls and the names its
    generated code refers to (tools/build_tpcds_env.py)."""
    global _TPCDS_ENV
    if _TPCDS_ENV is None:
        try:
            with gzip.open(TPCDS_ENV_DATA, "rb") as fh:
                _TPCDS_ENV = pickle.load(fh)
        except Exception as error:
            log(f"no TPC-DS ENV data ({error!r}); ENV edits keep the test")
            _TPCDS_ENV = False
    return _TPCDS_ENV or None


_TPCDS_CMP = None


def load_tpcds_cmp():
    """Per comparison or and/or site test_execute_tpcds evaluates, the TPC-DS queries that
    evaluated it and those where its neighbouring operator would have given another result
    (tools/build_tpcds_cmp.py)."""
    global _TPCDS_CMP
    if _TPCDS_CMP is None:
        try:
            with gzip.open(TPCDS_CMP_DATA, "rb") as fh:
                data = pickle.load(fh)
            if data.get("format") != 1 or data.get("test_id") != TPCDS_TEST:
                raise ValueError("unexpected format")
            _TPCDS_CMP = data
        except Exception as error:
            log(f"no per-query comparison data ({error!r}); comparison edits replay every query they reach")
            _TPCDS_CMP = False
    return _TPCDS_CMP or None


def load_import_lambdas():
    """Per file, the first lines of the lambdas called before any test (while modules are
    imported, dialects instantiated, or TPC-DS tables set up; tools/build_import_lambdas.py)."""
    global _IMPORT_LAMBDAS
    if _IMPORT_LAMBDAS is None:
        try:
            with gzip.open(IMPORT_LAMBDAS_DATA, "rb") as fh:
                _IMPORT_LAMBDAS = {r: set(v) for r, v in pickle.load(fh)["called"].items()}
        except Exception as error:
            log(f"no import-time lambda data ({error!r}); lambda edits keep the statement-level rules")
            _IMPORT_LAMBDAS = False
    return _IMPORT_LAMBDAS if _IMPORT_LAMBDAS is not False else None


def load_attr_owners():
    """Per class constant, the tests whose reads resolved to it (tools/attrs_plugin.py), packed
    per source file; loaded on first use."""
    global _ATTR_OWNERS
    if _ATTR_OWNERS is None:
        try:
            with gzip.open(ATTR_OWNERS_DATA, "rb") as fh:
                _ATTR_OWNERS = Blobs(pickle.load(fh)["by_file"])
        except Exception as error:
            log(f"no class-constant read data ({error!r}); such edits keep the name-level reads")
            _ATTR_OWNERS = False
    return _ATTR_OWNERS or None


def load_defaults():
    """Per function, the tests in which a call left each defaulted parameter at its default
    (tools/defaults_plugin.py), packed per source file; loaded on first use."""
    global _DEFAULTS
    if _DEFAULTS is None:
        try:
            with gzip.open(DEFAULTS_DATA, "rb") as fh:
                data = pickle.load(fh)
            _DEFAULTS = Blobs(data["by_file"])
        except Exception as error:
            log(f"no default-argument data ({error!r}); default edits keep every caller")
            _DEFAULTS = False
    return _DEFAULTS or None


def load_operands():
    """Recorded comparison operands (tools/operand_plugin.py), loaded on first use."""
    global _OPERANDS
    if _OPERANDS is None:
        try:
            with gzip.open(OPERANDS_DATA, "rb") as fh:
                _OPERANDS = Operands(pickle.load(fh))
        except Exception as error:
            log(f"no operand profile ({error!r}); comparison edits keep every test that ran them")
            _OPERANDS = False
    return _OPERANDS or None


def load_dictreads():
    """Per-key reads of wrapped dict literals (tools/operand_plugin.py), loaded on first use."""
    global _DICTREADS
    if _DICTREADS is None:
        try:
            with gzip.open(DICTREADS_DATA, "rb") as fh:
                _DICTREADS = Operands(pickle.load(fh))
        except Exception as error:
            log(f"no dict-read profile ({error!r}); such edits keep the wider selection")
            _DICTREADS = False
    return _DICTREADS or None


def load_imports():
    """Function-body lines that run while other modules are imported, with the modules that
    affects (tools/build_import_users.py)."""
    global _IMPORTS
    if _IMPORTS is None:
        try:
            with gzip.open(IMPORTS_DATA, "rb") as fh:
                _IMPORTS = pickle.load(fh)
        except Exception as error:
            log(f"no import-time data ({error!r}); function bodies run at import select every test")
            _IMPORTS = False
    return _IMPORTS or None


def load_argreq():
    """Per (expression class, arg): tests that checked it while missing (tools/build_argreq.py)."""
    global _ARGREQ
    if _ARGREQ is None:
        try:
            with gzip.open(ARGREQ_DATA, "rb") as fh:
                _ARGREQ = pickle.load(fh)
        except Exception as error:
            log(f"no arg_types data ({error!r}); such flips keep the class-wide selection")
            _ARGREQ = False
    return _ARGREQ or None


def load_cmp():
    """Weak-mutation data for comparisons, at the pinned commit (tools/build_cmp.py)."""
    global _CMP
    if _CMP is None:
        try:
            with gzip.open(CMP_DATA, "rb") as fh:
                _CMP = pickle.load(fh)
        except Exception as error:
            log(f"no comparison data ({error!r}); operator swaps keep the line-coverage selection")
            _CMP = False
    return _CMP or None


def load_dictkeys():
    """Per-key reads of class-level dict constants, at the pinned commit (tools/build_dictkeys.py)."""
    global _DICTKEYS
    if _DICTKEYS is None:
        try:
            with gzip.open(DICTKEYS_DATA, "rb") as fh:
                data = pickle.load(fh)
            if data.get("format") != 3:
                raise ValueError("expected the per-name packing (tools/build_lazy.py)")
            data["tracked_set"] = set(data["tracked"])
            data["keys"] = Blobs(data["keys"])  # name -> {key: test mask}
            if "keys_owned" in data:
                data["keys_owned"] = Blobs(data["keys_owned"])  # name -> {owner: {key: test mask}}
            _DICTKEYS = data
        except Exception as error:
            log(f"no dict-key index ({error!r}); such edits keep the class-wide selection")
            _DICTKEYS = False
    return _DICTKEYS or None
GENMEM_MODULE_S = 0.004  # importing one sqlglot module in the child (measured ~1-2 ms), with margin


def load_genmem():
    global _GENMEM
    if _GENMEM is None:
        try:
            with gzip.open(GENMEM_DATA, "rb") as fh:
                data = pickle.load(fh)
            data["loaded_sets"] = {d: set(v) for d, v in data["loaded"].items()}
            data["module_loaded_set"] = set(data["module_loaded"])
            _GENMEM = data
        except Exception as error:
            log(f"no generator-memory data ({error!r}); that test runs whenever it's selected")
            _GENMEM = False
    return _GENMEM or None


def load_globals():
    """Where each module-level name is read, at the pinned commit (tools/build_globals.py)."""
    global _GLOBALS
    if _GLOBALS is None:
        try:
            with gzip.open(GLOBALS_DATA, "rb") as fh:
                _GLOBALS = pickle.load(fh)
        except Exception as error:
            log(f"no module-constant index ({error!r}); such edits keep the broad selection")
            _GLOBALS = False
    return _GLOBALS or None


def load_tpcds():
    """Per-query coverage, CPU times and pristine outputs of test_execute_tpcds, recorded at the
    pinned commit (tools/build_tpcds.py). Loaded once in the parent; forked checks share it."""
    global _TPCDS
    if _TPCDS is None:
        try:
            with gzip.open(TPCDS_DATA, "rb") as fh:
                data = pickle.load(fh)
            data["setup_sets"] = {r: set(v) for r, v in data["setup_lines"].items()}
            data["digest_sets"] = {r: set(v) for r, v in data["digest_lines"].items()}

            def union(*parts):
                out = {}
                for part in parts:
                    for r, v in part.items():
                        out.setdefault(r, set()).update(v)
                return out

            for q in data["queries"]:
                if data.get("contexts", 1) >= 2:  # tools/build_tpcds_ctx.py
                    q["front_sets"] = union(q["parse_lines"], q["opt_lines"], q["duck_lines"])
                    q["log_sets"] = union(q["log_lines"])
                    q["exec_sets"] = union(q["exec_lines"])
                else:  # everything outside optimize() needs a full run
                    q["front_sets"] = union(q["opt_lines"])
                    q["log_sets"] = {}
                    q["exec_sets"] = union(q["rest_lines"])
                for k in ("opt_lines", "rest_lines", "parse_lines", "duck_lines", "log_lines", "exec_lines"):
                    q.pop(k, None)  # the forked checks don't need the lists
            _TPCDS = data
        except Exception as error:
            log(f"no TPC-DS data ({error!r}); the test runs whenever it's selected")
            _TPCDS = False
    return _TPCDS or None


def enter_overlay(patched):
    """In a forked child: serve the patched sources to this Python and to any it starts."""
    import tempfile

    src = str(SRC)
    overlay = tempfile.mkdtemp(prefix="br-")
    for rel, text in patched.items():
        path = os.path.join(overlay, "files", rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(text)
    with open(os.path.join(overlay, "sitecustomize.py"), "w") as fh:
        fh.write(OVERLAY_HOOK)
    # any Python this child starts (subprocess tests) loads the patched files too
    os.environ["PYTHONPATH"] = overlay + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    os.environ["BR_OVERLAY"] = overlay
    os.environ["BR_SRC_ROOT"] = src
    os.environ["SKIP_INTEGRATION"] = "0"
    exec(compile(OVERLAY_HOOK, "<overlay>", "exec"), {"__name__": "br_overlay"})
    sys.path.insert(0, src)
    os.chdir(src)
    return overlay


def child_tpcds(patched, plan, wfd):
    """In a forked child: replay the planned TPC-DS queries on the patched sources the way
    test_execute_tpcds runs them (parse, pickle to the worker, execute, pickle back, plus the
    duckdb transpilation) and report the test as failing unless every output is identical.

    "front" and "log" queries (changed code runs before execute()'s planning for them): a
    first pass stops each right after optimize() and compares a digest of its output; for
    "log" ones it also renders the output as execute()'s debug log line does, which must
    not raise or change it. Queries whose optimizer output is unchanged are settled, since
    nothing after that runs changed code. If some changed, a second pass runs just those
    to the end, reusing the first pass's optimizer output, and compares their rows,
    provided that fits the budget. "full" queries run to the end in the first pass. The
    child gives up (and the test stays selected) once its own CPU passes the budget."""
    import hashlib
    import shutil
    import signal
    import time

    import gc
    gc.set_threshold(*CHILD_GC)
    budget = plan.get("budget", float("inf"))
    if budget != float("inf"):
        # a hard stop: a changed plan can make one query run far longer than recorded
        signal.signal(signal.SIGPROF, lambda *_: os._exit(3))
        signal.setitimer(signal.ITIMER_PROF, budget)
    overlay = enter_overlay(patched)
    refresh_modules(patched)
    data = load_tpcds()
    from sqlglot import find_tables, parse_one, transpile
    import sqlglot.executor as executor_module
    from sqlglot.executor import execute
    from sqlglot.executor.table import Table

    if plan.get("guard"):
        # the patched sources imported (sqlglot.executor.env with them), which is what the
        # commit's narrowed selection waits for: from here on, any failure is a verdict
        # (the test stays selected), not a reason to fall back
        import sqlglot.executor.env  # noqa: F401

        def give_up(*_):
            os.write(wfd, json.dumps([TPCDS_TEST]).encode())
            os._exit(0)

        if budget != float("inf"):
            signal.signal(signal.SIGPROF, give_up)
    tables = {t: Table(columns=c, rows=data["tables"][t]) for t, c in data["schema"].items()}
    schema, tables = pickle.loads(pickle.dumps((data["schema"], tables)))
    by_index = {q["index"]: q for q in data["queries"]}
    front = plan["mode"] == "front"
    # per query: "front" (stop after optimize() and compare its digest), "log" (also render
    # the optimized SQL as execute()'s debug log line does), or "full" (run to the end)
    modes = plan.get("modes") or {i: "front" if front else "full" for i in plan["queries"]}
    debug = bool(os.environ.get("BR_DEBUG"))
    original_optimize = executor_module.optimize
    current = {}
    optimized = {}  # first-pass optimizer output of the queries whose digest changed

    class Stop(BaseException):
        pass

    from sqlglot.serde import META, TYPE

    def strip_meta(payloads):
        """serde.dump() payloads without node metadata (values left as they are)."""
        out = []
        for p in payloads:
            p = {k: v for k, v in p.items() if k != META}
            if TYPE in p:
                p[TYPE] = strip_meta(p[TYPE])
            out.append(p)
        return out

    def optimize_hook(*args, **kwargs):
        q = current["q"]
        if q["index"] in optimized:  # second pass: carry on from the first pass's output
            return optimized.pop(q["index"])
        result = original_optimize(*args, **kwargs)
        if modes.get(q["index"]) == "full":
            return result
        payloads = result.dump()
        current["digest"] = hashlib.sha256(repr(payloads).encode()).hexdigest()
        changed = current["digest"] != q["opt_digest"]
        if changed and q.get("meta_blind") and q.get("opt_digest_nometa") is not None and \
                hashlib.sha256(repr(strip_meta(payloads)).encode()).hexdigest() == q["opt_digest_nometa"]:
            # Only node metadata differs, and for this query nothing after optimize() (the
            # log line, planning, execution) ever reads metadata (tools/build_tpcds_meta.py),
            # so the rest of the run takes the same path and gives the same rows.
            changed = False
        if changed:
            optimized[q["index"]] = result  # re-run to the end later, from this output
        elif modes.get(q["index"]) == "log":
            # Unchanged output, but changed code renders it next: execute() evaluates
            # expression.sql(pretty=True) for a debug log line, whose text is dropped. That
            # must neither raise nor alter the expression the plan is then built from.
            try:
                result.sql(pretty=True)
                current["log_ok"] = hashlib.sha256(repr(result.dump()).encode()).hexdigest() == current["digest"]
            except Exception:
                current["log_ok"] = False
        raise Stop

    def spent():
        return time.process_time()  # this forked child's own CPU, imports included

    def run(q):
        """One query the way the test's pool worker runs it; True if its rows are unchanged."""
        current.clear()
        current["q"] = q
        expression = pickle.loads(pickle.dumps(parse_one(q["sql"])))
        used = {t.name: tables[t.name] for t in find_tables(expression)}
        try:
            result = pickle.loads(pickle.dumps(execute(expression, schema=schema, tables=used)))
        except Stop:
            return None if current.get("log_ok", True) else False
        same_rows = tuple(result.columns) == q["columns"] and list(result.rows) == q["rows"]
        if debug and not same_rows:
            diffs = [(a, b) for a, b in zip(result.rows, q["rows"]) if a != b][:2]
            current["rows_debug"] = (len(result.rows), len(q["rows"]), tuple(result.columns) == q["columns"], diffs)
        return same_rows

    same = True
    early = 0
    if any(m != "full" for m in modes.values()):
        executor_module.optimize = optimize_hook
    try:
        for i in plan["queries"]:
            q = by_index[i]
            if transpile(q["sql"], write="duckdb")[0] != q["duck_sql"]:
                same = False  # the expected side of the test's comparison moved
                break
            if run(q) is False:  # full mode: rows changed (front mode always stops early)
                same = False
                break
            if q["index"] in optimized and early < TPCDS_EARLY_RERUNS:
                # the first few changed plans run to the end right away: when the edit breaks
                # results, this is where it shows, long before the first pass would finish
                early += 1
                if run(q) is not True:
                    same = False
                    break
            if spent() > budget:
                same = False
                break
            if optimized and sum(max(by_index[j]["cpu"] - by_index[j]["front_cpu"], 0.0) for j in optimized) \
                    > TPCDS_RERUN_SHARE * (budget - spent()):
                # the second pass below would already refuse these reruns, and more changed
                # plans or more CPU spent only make that surer: stop here and keep the test
                same = False
                break
        if debug:
            print(f"tpcds child: {plan.get('commit')} first pass same={same} spent={spent():.2f}s changed_plans={len(optimized)} "
                  f"budget={budget:.2f}s", file=sys.stderr)
        if same and optimized:
            todo = sorted((by_index[i] for i in optimized), key=lambda q: q["cpu"] - q["front_cpu"])
            rerun = sum(max(q["cpu"] - q["front_cpu"], 0.0) for q in todo)
            if rerun > TPCDS_RERUN_SHARE * (budget - spent()):
                same = False  # too many changed plans to afford, with room for them running slower
            for q in todo if same else ():
                if run(q) is not True or spent() > budget:
                    if debug:
                        print(f"tpcds child: query {q['index']} differs or over budget, spent={spent():.2f}s "
                              f"rows={current.get('rows_debug')}", file=sys.stderr)
                    same = False
                    break
    except Exception:
        if not plan.get("guard"):
            raise
        same = False  # a query raised: the test fails
    finally:
        executor_module.optimize = original_optimize
    if debug:
        print(f"tpcds child: {plan.get('commit')} same={same} spent={spent():.2f}s", file=sys.stderr)
    shutil.rmtree(overlay, ignore_errors=True)
    if plan.get("keep"):
        same = False  # only the import was checked
    os.write(wfd, json.dumps([] if same else [TPCDS_TEST]).encode())
    os.close(wfd)


FIXTURE_PARSE_S = 0.05  # one case's parse and pickle
FIXTURE_DUCK_S = 1.2  # numpy, pandas and duckdb imports, the views and one duckdb query


def fixture_plan(checks, chosen, duration, number_of):
    """Which "exec" readers to clear by checking their changed cases, or None.

    test_execute_tpc* parse every case's input, pickle it to a pool worker and, if its
    meta says execute, run sqlglot's executor there and compare the rows with duckdb
    running the duckdb transpilation of the same input. A changed case is re-checked
    exactly that way; a case that doesn't execute only needs to parse and pickle. Only
    TPC-DS cases can be executed here (its tables are in tpcds.pkl.gz)."""
    tests, estimate, duck = {}, 0.5, False
    data = None
    for test_id, cases in sorted(checks.items()):
        number = number_of.get(test_id)
        if number is None or number not in chosen:
            continue
        cost, ok = 0.0, True
        for rel, i, meta, sql, old in cases:
            cost += FIXTURE_PARSE_S
            if meta.get("execute"):
                if test_id != TPCDS_TEST:
                    ok = False
                    break
                data = data or load_tpcds()
                q = data and next((q for q in data["queries"] if q["index"] == i), None)
                cost += 2 * (q["cpu"] if q else 1.0) + (0.0 if duck else FIXTURE_DUCK_S)
                duck = True
        if not ok:
            continue
        rest = [n for n in chosen if n != number]
        saving = (pack_cost(chosen, duration) - pack_cost(rest, duration)) / 1000
        if cost > FIXTURE_GATE * saving:
            continue
        tests[test_id] = cases
        estimate += cost
    if not tests:
        return None
    saving = (pack_cost(chosen, duration)
              - pack_cost([n for n in chosen if n not in {number_of[t] for t in tests}], duration)) / 1000
    log(f"fixture case check planned: {sum(len(c) for c in tests.values())} cases of {len(tests)} tests, "
        f"est {estimate:.1f}s to save {saving:.1f}s")
    return {"tests": tests, "estimate": estimate, "budget": max(estimate * 3 + 1.0, 0.5 * saving)}


def child_fixture(patched, plan, wfd):
    """In a forked child: each changed case of a test_execute_tpc* test, on the patched sources,
    the way the test runs it: parse_one(input), pickled to the worker; if its meta says
    execute, sqlglot's executor on the TPC-DS tables there, pickled back, against duckdb
    running transpile(input, write="duckdb") over the same CSV files, compared with the
    test's own assert_frame_equal call. A test passes only if every changed case does
    (unchanged cases pass as they did: whatever the rest of the commit changes didn't
    select the test). Stops itself past the budget, which keeps every test."""
    import pickle
    import resource
    import shutil
    import signal

    import time

    signal.signal(signal.SIGPROF, lambda *_: os._exit(3))
    signal.setitimer(signal.ITIMER_PROF, plan["budget"])
    overlay = enter_overlay(patched)
    refresh_modules(patched)
    from sqlglot import find_tables, parse_one, transpile

    state = {}
    debug = bool(os.environ.get("BR_DEBUG"))

    def tpcds():
        if "schema" not in state:
            data = load_tpcds()
            state["schema"] = pickle.loads(pickle.dumps(data["schema"]))
            state["tables"] = {}
            state["queries"] = {q["index"]: q for q in data["queries"]}
        return state

    def table_of(name):
        """A table as the worker holds it (built by setUpClass, pickled to the pool); only the
        ones a query names are built, since execute() is only handed those."""
        st = tpcds()
        if name not in st["tables"]:
            from sqlglot.executor.table import Table

            data = load_tpcds()
            st["tables"][name] = pickle.loads(pickle.dumps(Table(columns=data["schema"][name], rows=data["tables"][name])))
        return st["tables"][name]

    def duck():
        if "conn" not in state:
            import duckdb
            from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA

            conn = duckdb.connect()
            conn.execute("SET threads TO 1")
            conn.execute("SET memory_limit = '1GB'")
            directory = FIXTURES_DIR + "/optimizer/tpc-ds/"
            for table, columns in TPCDS_SCHEMA.items():  # as TestExecutor.setUpClass
                file_name = f"{directory}{table}.csv.gz"
                conn.execute(
                    f"""
                CREATE VIEW {table} AS
                SELECT *
                FROM READ_CSV('{file_name}', delim='|', header=True, columns={columns})
                """
                )
            state["conn"] = conn
        return state["conn"]

    def executed(expression):
        """mp_execute in the pool worker, under a memory cap: a broken query can blow up."""
        from sqlglot.executor import execute

        st = tpcds()
        names = [t.name for t in find_tables(expression)]
        for name in names:
            if name not in st["schema"]:
                raise KeyError(name)  # as _tables[name] in mp_execute
        used = {name: table_of(name) for name in names}
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        try:
            try:  # a soft cap only, so it can be lifted again for duckdb
                with open("/proc/self/status") as fh:
                    size = next(int(line.split()[1]) << 10 for line in fh if line.startswith("VmSize:"))
                cap = size + CHILD_MEMORY
                resource.setrlimit(resource.RLIMIT_AS, (cap if hard == resource.RLIM_INFINITY else min(cap, hard), hard))
            except Exception:
                pass
            table = execute(expression, schema=st["schema"], tables=used)
        finally:
            try:
                resource.setrlimit(resource.RLIMIT_AS, (soft, hard))
            except (ValueError, OSError):
                pass
        return pickle.loads(pickle.dumps(table))

    def passes(test_id, i, meta, sql, old):
        expression = pickle.loads(pickle.dumps(parse_one(sql)))
        if not meta.get("execute"):
            return True
        if test_id != TPCDS_TEST:
            return False
        if old is not None and old[0].get("execute") and expression.dump() == parse_one(old[1]).dump():
            return True  # the same expression: every later step sees what it saw before
        table = executed(expression)
        duck_sql = transpile(sql, write="duckdb")[0]
        if debug:
            print(f"fixture case {i}: executed at {time.process_time():.2f}s", file=sys.stderr)
        q = tpcds()["queries"].get(i)
        if q is not None and old is not None and q["sql"] == old[1] and duck_sql == q["duck_sql"] \
                and tuple(table.columns) == q["columns"] and list(table.rows) == q["rows"]:
            return True  # both sides of the comparison are what they were when it passed
        cursor = duck().execute(duck_sql)
        if len(cursor.description) != len(table.columns):
            return False  # assert_frame_equal fails on the shape before it looks at values
        rows = cursor.fetchall()
        if len(rows) != len(table.rows):
            return False
        if debug:
            print(f"fixture case {i}: same shape, comparing frames at {time.process_time():.2f}s", file=sys.stderr)
        import numpy as np
        import pandas as pd
        from pandas.testing import assert_frame_equal

        a = duck().execute(duck_sql).fetchdf()
        b = pd.DataFrame(((np.nan if c is None else c for c in r) for r in table.rows), columns=table.columns)
        assert_frame_equal(a, b, check_dtype=False, check_index_type=False)
        return True

    failed = []
    for test_id, cases in plan["tests"].items():
        for rel, i, meta, sql, old in cases:
            try:
                ok = passes(test_id, i, meta, sql, old)
            except BaseException as error:  # the test raises here too
                if os.environ.get("BR_DEBUG"):
                    print(f"fixture case {test_id} {i}: {error!r}"[:300], file=sys.stderr)
                ok = False
            if debug:
                print(f"fixture case {test_id} {i}: {'passes' if ok else 'fails'} at {time.process_time():.2f}s",
                      file=sys.stderr)
            if not ok:
                failed.append(test_id)
                break
    shutil.rmtree(overlay, ignore_errors=True)
    os.write(wfd, json.dumps(failed).encode())
    os.close(wfd)


def child_genmem(patched, plan, wfd):
    """In a forked child: test_generator_memory's body, on the patched sources, for the planned
    dialects only (in the test's order). Reports the test as failing if any of them leaks."""
    import gc
    import shutil
    import signal
    import tracemalloc

    signal.signal(signal.SIGPROF, lambda *_: os._exit(3))
    signal.setitimer(signal.ITIMER_PROF, plan["budget"])
    overlay = enter_overlay(patched)
    refresh_modules(patched)
    import importlib

    importlib.import_module("tests.test_generator")  # an import error fails the test (and this child)
    from sqlglot.dialects.dialect import Dialect, Dialects

    wanted = set(plan["dialects"])
    ok = True
    for name in Dialects:
        if name.value not in wanted:
            continue
        dialect = Dialect.get_or_raise(name)
        for _ in range(100):
            dialect.generator()
        gc.collect()
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            for _ in range(1000):
                dialect.generator()
            gc.collect()
            after = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        if not after - before < 1024:
            ok = False
            break
    shutil.rmtree(overlay, ignore_errors=True)
    os.write(wfd, json.dumps([] if ok else [GENMEM_TEST]).encode())
    os.close(wfd)


def child_run(patched, test_ids, limits, wfd):
    """Runs in a forked child: import patched sqlglot, run the tests, report failures."""
    import importlib
    import shutil
    import time
    import unittest

    import gc
    gc.set_threshold(*CHILD_GC)
    overlay = enter_overlay(patched)
    refresh_modules(patched)
    src = str(SRC)

    # process pools fork instead of spawn, so workers keep the patched modules
    import multiprocessing
    import multiprocessing.context
    real_get_context = multiprocessing.get_context

    def get_context(method=None):
        return real_get_context("fork" if method in (None, "spawn", "forkserver") else method)

    multiprocessing.get_context = get_context
    cpus = min(2, os.cpu_count() or 2)
    os.cpu_count = lambda: cpus
    if hasattr(os, "process_cpu_count"):
        os.process_cpu_count = lambda: cpus

    failed = set()

    class Result(unittest.TestResult):
        def _mark(self, test, err=None):
            if os.environ.get("BR_DEBUG") and err:
                import traceback
                print(test, "".join(traceback.format_exception(*err))[-600:], file=sys.stderr)
            tid = getattr(test, "test_id", None)
            if tid is None:
                case = getattr(test, "test_case", None)
                tid = getattr(case, "test_id", None)
            if tid is None:  # class or module setup failed: blame everything we ran
                failed.update(test_ids)
            else:
                failed.add(tid)

        def startTest(self, test):
            super().startTest(test)
            self._t0 = time.perf_counter()

        def stopTest(self, test):
            super().stopTest(test)
            tid = getattr(test, "test_id", None)
            # far slower than recorded: it may have timed out where it was labeled
            if tid is not None and time.perf_counter() - self._t0 > 2 + 5 * limits.get(tid, 0):
                failed.add(tid)

        def addError(self, test, err):
            self._mark(test, err)

        def addFailure(self, test, err):
            self._mark(test, err)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self._mark(test, err)

        def addUnexpectedSuccess(self, test):
            self._mark(test)

    prof = os.environ.get("BR_PROFILE")

    def mark(what):
        if prof:
            os.write(3 if False else 2, f"  [{os.getpid()}] {time.process_time():.2f} {what}\n".encode())

    mark("setup done")
    groups = {}
    for tid in test_ids:
        f, cls, meth = tid.split("::")
        groups.setdefault(f, {}).setdefault(cls, []).append((meth, tid))
    for f, classes in groups.items():
        modname = f[:-3].replace("/", ".")
        try:
            mod = importlib.import_module(modname)
        except BaseException:
            if os.environ.get("BR_DEBUG"):
                import traceback
                traceback.print_exc()
            failed.update(t for ms in classes.values() for _, t in ms)
            continue
        mark(f"imported {modname}")
        for cls_name, meths in classes.items():
            cls = getattr(mod, cls_name, None)
            if cls is None:
                failed.update(t for _, t in meths)
                continue
            cases = []
            for meth, tid in meths:
                try:
                    case = cls(meth)
                except BaseException:
                    failed.add(tid)
                    continue
                case.test_id = tid
                cases.append(case)
            result = Result()
            try:
                unittest.TestSuite(cases).run(result)
            except BaseException:
                failed.update(t for _, t in meths)
            mark(f"ran {cls_name} x{len(cases)}")
    shutil.rmtree(overlay, ignore_errors=True)
    os.write(wfd, json.dumps(sorted(failed)).encode())
    os.close(wfd)


PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
# young collections in a check child: tests allocate millions of short-lived nodes, and the
# default threshold collects every 2000 of them (measured: ~5% of a check's CPU)
CHILD_GC = (20000, 20, 20)
HEAVY_FILES = {"tests/test_optimizer.py", "tests/test_executor.py"}  # import duckdb, numpy, pandas


def warm(jobs):
    """Import, once in the parent, what every check would otherwise import again after forking.
    None of it depends on sqlglot, so the children still load the patched sources."""
    import importlib
    import site

    site.main()
    names = ("typing collections re itertools logging datetime decimal functools enum json copy "
             "string inspect gc threading tempfile shutil pathlib multiprocessing multiprocessing.context "
             "concurrent.futures dataclasses contextlib ast unittest unittest.mock difflib heapq csv "
             "sqlite3 importlib.metadata importlib.abc importlib.util select signal zoneinfo uuid hashlib "
             "textwrap pprint operator numbers weakref types").split()
    if any(t.split("::")[0] in HEAVY_FILES for job in jobs
           if "__tpcds__" not in job[3] and "__genmem__" not in job[3] and "__fixture__" not in job[3]
           for t in job[2]):
        names += ["numpy", "pandas", "pandas.testing", "duckdb"]
    for name in names:
        try:
            importlib.import_module(name)
        except Exception:
            pass


# A TPC-DS replay child may allocate at most this much beyond what it inherited: the run has
# 4 GB for everything, two checks run at once, and an edit that breaks a plan can make one
# query's intermediate results explode. Past it, allocations fail and the replay fails,
# which keeps the test. (Not for test checks: duckdb reserves address space it never uses.)
CHILD_MEMORY = int(os.environ.get("BR_CHILD_MEMORY", str(1280 << 20)))


def limit_memory():
    import resource

    try:
        with open("/proc/self/status") as fh:
            size = next(int(line.split()[1]) << 10 for line in fh if line.startswith("VmSize:"))
        cap = size + CHILD_MEMORY
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    except Exception:
        pass


# Importing sqlglot costs each forked check about 0.15 s of CPU, more with the test modules.
# The parent imports the pristine sources once and each check drops and imports again only
# the modules its patch can reach: the patched ones, every module whose import was still
# running when a patched one started (its import could have run the old code), and every
# module that refers to a dropped one. Dropped sqlglot modules are imported again at once
# so registries such as Dialect._classes hold the new classes. If a kept module still holds
# an old object afterwards, or a non-Python file changed, everything is dropped, which is
# the same as a fresh import.

ZYGOTE = os.environ.get("BR_ZYGOTE", "1") != "0"
_IMPORT_ORDER = {}  # module -> (start tick, end tick) of its import in the parent
_OURS = ("sqlglot", "tests")


def preimport(jobs):
    import importlib
    import importlib.abc
    import importlib.machinery
    import itertools

    if not ZYGOTE:
        return
    files = sorted({t.split("::")[0] for job in jobs
                    if "__tpcds__" not in job[3] and "__genmem__" not in job[3] and "__fixture__" not in job[3]
                    for t in job[2]})
    tick = itertools.count()

    class Watch(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname.split(".")[0] not in _OURS:
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            loader = getattr(spec, "loader", None)
            if loader is None or not hasattr(loader, "exec_module"):
                return spec
            real = loader.exec_module

            def exec_module(module):
                start = next(tick)
                real(module)
                _IMPORT_ORDER[fullname] = (start, next(tick))

            loader.exec_module = exec_module
            return spec

    src, cwd = str(SRC), os.getcwd()
    os.environ["SKIP_INTEGRATION"] = "0"  # as in the checks: tests read it at import
    sys.path.insert(0, src)
    os.chdir(src)
    watch = Watch()
    sys.meta_path.insert(0, watch)
    try:
        names = ["sqlglot"]
        if any("__genmem__" in job[3] for job in jobs):
            names += ["tests.test_generator"]
        if any("__tpcds__" in job[3] or "__fixture__" in job[3] for job in jobs):
            names += ["sqlglot.executor", "sqlglot.optimizer"]
        names += [f[:-3].replace("/", ".") for f in files]
        for name in names:
            try:
                importlib.import_module(name)
            except BaseException:
                pass
    finally:
        sys.meta_path.remove(watch)
        os.chdir(cwd)


def _module_refs(name, module):
    """Names of the modules whose objects this module's globals hold (one level into containers)."""
    import types

    out = set()
    is_pkg = hasattr(module, "__path__")

    def add(v):
        if isinstance(v, types.ModuleType):
            out.add(v.__name__)
        elif isinstance(v, type):
            for b in v.__mro__:
                out.add(b.__module__)
        elif isinstance(v, (types.FunctionType, types.BuiltinFunctionType)):
            out.add(getattr(v, "__module__", None))
        else:
            out.add(type(v).__module__)

    for k, v in list(vars(module).items()):
        if is_pkg and isinstance(v, types.ModuleType) and v.__name__ == f"{name}.{k}":
            continue  # the binding the import system makes for a submodule
        add(v)
        if isinstance(v, dict):
            for x in list(v.keys()) + list(v.values()):
                add(x)
        elif isinstance(v, (list, tuple, set, frozenset)):
            for x in v:
                add(x)
    return out


def _holds_any(module, name, ids):
    """Whether a module's globals, its own classes' attributes, or containers one level
    inside either, hold an object whose id is in ids."""
    def hit(v):
        if id(v) in ids:
            return True
        if isinstance(v, dict):
            return any(id(x) in ids for x in v.values()) or any(id(x) in ids for x in v)
        if isinstance(v, (list, tuple, set, frozenset)):
            return any(id(x) in ids for x in v)
        return False

    for v in list(vars(module).values()):
        if hit(v):
            return True
        if isinstance(v, type) and v.__module__ == name:
            for a in list(vars(v).values()):
                if hit(a):
                    return True
    # metaclass attributes (Dialect._classes lives on _Dialect)
    return False


def refresh_modules(patched):
    """In a forked check, after enter_overlay: make the pre-imported modules match the patch."""
    import importlib
    import types

    ours = {n: m for n, m in list(sys.modules.items()) if n.split(".")[0] in _OURS}
    if not ours:
        return
    src = os.path.realpath(str(SRC))
    by_file = {}
    for n, m in ours.items():
        f = getattr(m, "__file__", None)
        if f:
            by_file[os.path.realpath(f)] = n
    whole = not _IMPORT_ORDER
    targets = set()
    for rel in patched:
        if not rel.endswith(".py"):
            whole = True
            break
        n = by_file.get(os.path.realpath(os.path.join(src, rel)))
        if n:
            targets.add(n)

    def drop_all(why):
        if os.environ.get("BR_DEBUG"):
            os.write(2, f"  refresh: dropping everything ({why})\n".encode())
        for n in [n for n in sys.modules if n.split(".")[0] in _OURS]:
            del sys.modules[n]

    if whole:
        drop_all("not Python" if _IMPORT_ORDER else "nothing pre-imported")
        return
    if not targets:
        if os.environ.get("BR_DEBUG"):
            os.write(2, b"  refresh: patched modules not loaded, kept all\n")
        return  # the patched modules were never imported here, so nothing holds their old code
    first = min(_IMPORT_ORDER[n][0] for n in targets if n in _IMPORT_ORDER) if any(
        n in _IMPORT_ORDER for n in targets) else -1
    drop = set(targets)
    for n in ours:
        o = _IMPORT_ORDER.get(n)
        if o is None or o[1] > first:
            drop.add(n)
    refs = {n: _module_refs(n, m) for n, m in ours.items() if n not in drop}
    grew = True
    while grew:
        grew = False
        for n, r in refs.items():
            if n not in drop and r & drop:
                drop.add(n)
                grew = True
    old = {n: sys.modules.pop(n) for n in drop}
    # A kept package still binds a dropped submodule as an attribute, and `from pkg import sub`
    # reads that attribute before sys.modules: unbind it, so that import loads the new module.
    for n, m in old.items():
        parent, _, leaf = n.rpartition(".")
        pkg = sys.modules.get(parent)
        if pkg is not None and vars(pkg).get(leaf) is m:
            delattr(pkg, leaf)
    for n in sorted(n for n in drop if n.split(".")[0] == "sqlglot"):
        if n not in sys.modules:
            importlib.import_module(n)  # an error here fails the check, which keeps its tests
    # A package imported again doesn't bind the submodules we kept. Only a missing name is
    # bound: the package's own code may have rebound it (sqlglot.expressions.array is both a
    # submodule and, after `from .builders import array`, a function).
    for n, m in list(sys.modules.items()):
        if n.split(".")[0] in _OURS and "." in n:
            parent, _, leaf = n.rpartition(".")
            pkg = sys.modules.get(parent)
            if pkg is not None and parent in drop and leaf not in vars(pkg):
                setattr(pkg, leaf, m)
    # Old objects of dropped test modules may stay registered (a test's own Dialect subclass in
    # Dialect._classes, say); the check imports that test module again, which registers it anew.
    old = {n: m for n, m in old.items() if n.split(".")[0] == "sqlglot"}
    old_ids = {id(m) for m in old.values()}
    for n, m in old.items():
        for v in list(vars(m).values()):
            if isinstance(v, (type, types.FunctionType)) and getattr(v, "__module__", None) == n:
                old_ids.add(id(v))
    # every sqlglot module now loaded, the ones just imported again included
    kept = [(n, m) for n, m in sys.modules.items() if n.split(".")[0] in _OURS and m not in old.values()]
    metas = {type(v) for _, m in kept for v in list(vars(m).values())
             if isinstance(v, type) and type(v) is not type}
    if any(_holds_any(m, n, old_ids) for n, m in kept) or any(
            any(id(x) in old_ids for x in (a.values() if isinstance(a, dict) else ()))
            for meta in metas for a in list(vars(meta).values())):
        drop_all("a kept module holds an old object")
    elif os.environ.get("BR_DEBUG"):
        os.write(2, f"  refresh: {len(targets)} patched, dropped {len(drop)} of {len(ours)}\n".encode())


def run_checks(jobs, budget_s):
    """jobs: (key, patched, test_ids, recorded seconds by id, limit_s). Returns {key: failed ids or None}."""
    import select
    import signal
    import time

    results = {}
    pending = list(jobs)
    running = {}
    deadline = time.monotonic() + budget_s
    while pending or running:
        while pending and len(running) < PARALLEL and time.monotonic() < deadline:
            key, patched, test_ids, limits, limit = pending.pop(0)
            r, w = os.pipe()
            sys.stdout.flush()
            sys.stderr.flush()
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(r)
                    devnull = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(devnull, 1)
                    if not os.environ.get("BR_DEBUG"):
                        os.dup2(devnull, 2)
                    if "__tpcds__" in limits:
                        limit_memory()  # only the replay: duckdb's own tests reserve far more address space
                        child_tpcds(patched, limits["__tpcds__"], w)
                    elif "__genmem__" in limits:
                        child_genmem(patched, limits["__genmem__"], w)
                    elif "__fixture__" in limits:
                        child_fixture(patched, limits["__fixture__"], w)
                    else:
                        child_run(patched, test_ids, limits, w)
                    os._exit(0)
                except BaseException:
                    if os.environ.get("BR_DEBUG"):
                        import traceback
                        traceback.print_exc()
                    os._exit(1)
            os.close(w)
            running[pid] = (key, r, time.monotonic() + limit, bytearray())
        if not running:
            for key, *_ in pending:
                results[key] = None
            break
        fds = {v[1]: pid for pid, v in running.items()}
        ready, _, _ = select.select(list(fds), [], [], 0.05)
        for fd in ready:
            pid = fds[fd]
            chunk = os.read(fd, 1 << 16)
            if chunk:
                running[pid][3].extend(chunk)
                continue
            key, _, _, buf = running.pop(pid)
            os.close(fd)
            _, status, ru = os.wait4(pid, 0)
            log(f"{key}: check used {ru.ru_utime + ru.ru_stime:.2f}s CPU")
            ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
            try:
                results[key] = set(json.loads(buf.decode())) if ok else None
            except ValueError:
                results[key] = None
        now = time.monotonic()
        for pid, (key, fd, until, _) in list(running.items()):
            if now > until or now > deadline:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                os.waitpid(pid, 0)
                os.close(fd)
                running.pop(pid)
                results[key] = None
                log(f"{key}: check timed out, keeping its tests")
    return results


# --------------------------------------------------------------------------- import-time state
#
# A changed function line that also ran while modules were imported (the parser runs while
# the DuckDB generator parses its SQL templates at import) can change what those imports
# build, so such an edit is normally widened to everything importing them. Most such edits
# leave every import-time structure as it was. To tell, a fresh interpreter imports every
# sqlglot module (and the test modules whose import runs sqlglot code), with and without the
# patch, and fingerprints all module and class state: values, containers, expression trees,
# function defaults and closures, objects reachable from them. Code is left out, since what
# runs later is what line coverage already decides. Equal fingerprints mean the edit only
# matters where its lines run in tests.

STATE_MIN_GAIN_MS = 3000  # the check costs ~0.8 s of CPU per commit, plus the pristine sources once
STATE_BUDGET_S = float(os.environ.get("BR_STATE_BUDGET", "40"))
STATE_WALL_S = 8.0  # a normal run takes under a second; an edit that loops at import doesn't get far


def _state_limits():
    """In the fingerprint process, before exec: a CPU and a memory cap (an edit can make an
    import-time parse loop or explode)."""
    import resource

    try:
        resource.setrlimit(resource.RLIMIT_CPU, (6, 7))
        resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
    except Exception:
        pass
STATE_SKIP_CLASS_KEYS = {"__dict__", "__weakref__", "__doc__", "__module__", "__qualname__", "__firstlineno__",
                         "__static_attributes__", "__annotations__", "__orig_bases__", "__parameters__",
                         "__annotate_func__", "__annotations_cache__"}


def _state_name(k):
    """A name for a hashable value that doesn't depend on memory addresses, or None."""
    import enum
    import types

    t = type(k)
    if t in (int, float, complex, str, bytes, bool, type(None)):
        return f"{t.__name__}:{k!r}"
    if isinstance(k, enum.Enum):
        return f"enum:{t.__module__}.{t.__qualname__}.{k.name}"
    if isinstance(k, type):
        return f"type:{k.__module__}.{k.__qualname__}<" + ",".join(
            f"{b.__module__}.{b.__qualname__}" for b in k.__mro__[1:4]) + ">"
    if isinstance(k, (types.FunctionType, types.BuiltinFunctionType)):
        return f"func:{getattr(k, '__module__', '')}.{getattr(k, '__qualname__', '')}"
    if isinstance(k, (tuple, frozenset)):
        names = [_state_name(e) for e in k]
        if any(n is None for n in names):
            return None
        return ("tuple(" + ",".join(names) + ")") if isinstance(k, tuple) else ("fset(" + ",".join(sorted(names)) + ")")
    return None


def _state_order(keys, strict=False):
    names = [_state_name(k) for k in keys]
    if None in names or len(set(names)) != len(names):
        return None if strict else range(len(keys))
    return sorted(range(len(keys)), key=lambda i: names[i])


class StateFP:
    PRIMS = (int, float, complex, str, bytes, bool, type(None))

    def __init__(self, expr_type, masks=()):
        self.memo = {}
        self.keep = []  # visited objects stay alive, so their ids stay unique
        self.expr = expr_type
        self.masks = set(masks)  # (module, class qualname, name): the edited binding itself

    @staticmethod
    def classes_sorted(elems):
        """A sequence made only of classes is usually a set's contents (`tuple({exp.Add, ...})`),
        whose order follows memory addresses: compare those by name."""
        if len(elems) > 1 and all(isinstance(e, type) for e in elems):
            order = _state_order(elems, strict=True)
            if order is not None:
                return [elems[j] for j in order]
        return elems

    @staticmethod
    def own(cls):
        mod = getattr(cls, "__module__", "") or ""
        return mod.startswith(("sqlglot", "tests"))

    def tok(self, x, out):
        import collections.abc
        import enum
        import functools
        import re
        import types

        t = type(x)
        if t in self.PRIMS:
            out.append(f"{t.__name__}:{x!r}")
            return
        if isinstance(x, enum.Enum):
            out.append(f"enum:{t.__module__}.{t.__qualname__}.{x.name}")
            return
        if isinstance(x, type) and not self.own(x):
            out.append(f"type:{x.__module__}.{x.__qualname__}")
            return
        if isinstance(x, types.ModuleType):
            out.append(f"module:{x.__name__}")
            return
        if isinstance(x, (types.BuiltinFunctionType, types.MethodDescriptorType, types.WrapperDescriptorType,
                          types.MethodWrapperType, types.GetSetDescriptorType, types.MemberDescriptorType)):
            out.append(f"builtin:{getattr(x, '__qualname__', t.__name__)}")
            return
        if isinstance(x, (tuple, frozenset)):
            # immutable: by value, never by identity (a module compiled from source and one
            # loaded from bytecode share constant tuples differently)
            if isinstance(x, frozenset):
                elems = list(x)
                order = _state_order(elems, strict=True)
                out.append(f"{t.__module__}.{t.__qualname__}<")
                if order is not None:
                    for j in order:
                        self.tok(elems[j], out)
                else:
                    parts = []
                    for v in elems:
                        sub = []
                        self.tok(v, sub)
                        parts.append("|".join(sub))
                    out.append(",".join(sorted(parts)))
                out.append(">")
            else:
                out.append(f"{t.__module__}.{t.__qualname__}[")
                for v in self.classes_sorted(list(x)):
                    self.tok(v, out)
                out.append("]")
            return
        i = id(x)
        if i in self.memo:
            out.append(f"ref:{self.memo[i]}")
            return
        self.memo[i] = len(self.memo)
        self.keep.append(x)
        if isinstance(x, type):  # a sqlglot (or test) class: its own attributes, once
            out.append(f"class:{x.__module__}.{x.__qualname__}(")
            self.tok(tuple(x.__mro__[1:]), out)
            for k, v in list(vars(x).items()):
                if k not in STATE_SKIP_CLASS_KEYS:
                    out.append(k)
                    if (x.__module__, x.__qualname__, k) in self.masks:
                        out.append("masked")
                        continue
                    self.tok(v, out)
            out.append(")")
            return
        if isinstance(x, types.FunctionType):
            out.append(f"func:{x.__module__}.{x.__qualname__}(")
            code = x.__code__
            if not code.co_filename.endswith(".py"):  # generated at import (exec): its code is state
                out.append(repr(code.co_code))
                self.tok(code.co_consts, out)
                self.tok(code.co_names, out)
            self.tok(x.__defaults__, out)
            self.tok(x.__kwdefaults__, out)
            for cell in x.__closure__ or ():
                try:
                    self.tok(cell.cell_contents, out)
                except ValueError:
                    out.append("emptycell")
            if x.__dict__:
                self.tok(x.__dict__, out)
            out.append(")")
            return
        if isinstance(x, types.CodeType):
            out.append(f"code:{x.co_qualname}")
            return
        if isinstance(x, types.MethodType):
            out.append("method(")
            self.tok(x.__self__, out)
            self.tok(x.__func__, out)
            out.append(")")
            return
        if isinstance(x, (staticmethod, classmethod)):
            out.append(f"{t.__name__}(")
            self.tok(x.__func__, out)
            out.append(")")
            return
        if isinstance(x, property):
            out.append("property(")
            for g in (x.fget, x.fset, x.fdel):
                self.tok(g, out)
            out.append(")")
            return
        if isinstance(x, functools.partial):
            out.append("partial(")
            self.tok(x.func, out)
            self.tok(x.args, out)
            self.tok(x.keywords, out)
            out.append(")")
            return
        if isinstance(x, re.Pattern):
            out.append(f"re:{x.pattern!r}:{x.flags}")
            return
        if isinstance(x, self.expr):
            out.append(f"expr:{t.__module__}.{t.__qualname__}(")
            self.tok(x.args, out)
            for attr in ("comments", "_type", "_meta"):
                self.tok(getattr(x, attr, None), out)
            out.append(")")
            return
        if isinstance(x, collections.abc.Mapping):
            out.append(f"{t.__module__}.{t.__qualname__}{{")
            items = list(x.items())
            for k, v in [items[j] for j in _state_order([k for k, _ in items])]:
                self.tok(k, out)
                self.tok(v, out)
            out.append("}")
        elif isinstance(x, (collections.abc.Set, set, frozenset)):
            elems = list(x)
            order = _state_order(elems, strict=True)
            out.append(f"{t.__module__}.{t.__qualname__}<")
            if order is not None:
                for j in order:
                    self.tok(elems[j], out)
            else:
                parts = []
                for v in elems:
                    sub = []
                    self.tok(v, sub)
                    parts.append("|".join(sub))
                out.append(",".join(sorted(parts)))
            out.append(">")
        elif isinstance(x, (list, tuple, collections.abc.Sequence, collections.deque)) and not isinstance(x, (str, bytes)):
            out.append(f"{t.__module__}.{t.__qualname__}[")
            for v in self.classes_sorted(list(x)):
                self.tok(v, out)
            out.append("]")
        else:
            out.append(f"obj:{t.__module__}.{t.__qualname__}")
        # whatever else the object holds
        d = getattr(x, "__dict__", None)
        slots = [s for c in t.__mro__ for s in (getattr(c, "__slots__", ()) or ()) if isinstance(s, str)]
        if isinstance(d, dict) or slots:
            out.append("(")
            if isinstance(d, dict):
                self.tok(d, out)
            for s in slots:
                if s not in ("__dict__", "__weakref__") and hasattr(x, s):
                    out.append(s)
                    try:
                        self.tok(getattr(x, s), out)
                    except Exception:
                        out.append("?")
            out.append(")")
        elif not isinstance(x, (collections.abc.Mapping, collections.abc.Set, collections.abc.Sequence, set,
                                frozenset, list, tuple, collections.deque)):
            r = repr(x)
            out.append(r if " at 0x" not in r and "0x" not in r else "opaque")


def state_digests(test_modules, masks=()):
    """Import every sqlglot module, then the given test modules; per module, a digest of its
    import-time state (see StateFP), and the modules that failed to import."""
    import hashlib
    import importlib
    import pkgutil

    import sqlglot

    names = sorted(m.name for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot.") if not m.name.endswith("__main__"))
    failed = []
    for name in names + sorted(test_modules):
        try:
            importlib.import_module(name)
        except BaseException as error:  # noqa: BLE001  a failure is part of the state
            failed.append(f"{name}: {type(error).__name__}")
    expr = getattr(sqlglot.exp, "Expr", None) or sqlglot.exp.Expression
    fp = StateFP(expr, masks)
    digests = {}
    for name in sorted(m for m in list(sys.modules) if m == "sqlglot" or m.startswith(("sqlglot.", "tests"))):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        out = []
        for k, v in sorted(vars(mod).items()):
            if k.startswith("__") and k.endswith("__"):
                continue
            out.append(k)
            fp.tok(v, out)
        digests[name] = hashlib.sha256("\x00".join(out).encode("utf-8", "surrogatepass")).hexdigest()
    return {"digests": digests, "failed": failed}


def state_main(args):
    """`selector.py --state <patched pickle> [test modules]`: print the import-time digests of
    the pinned sources with that patch (an empty one for the pristine state)."""
    import shutil
    import site
    import threading

    path, test_modules = args[0], args[1:]
    with open(path, "rb") as fh:
        job = pickle.load(fh)
    patched, masks = job["patched"], [tuple(m) for m in job.get("masks", ())]
    site.main()  # the check children see site-packages too (warm() adds them)
    overlay = enter_overlay(patched)
    result = {}

    def run():
        try:
            result["state"] = state_digests(test_modules, masks)
        except BaseException as error:  # noqa: BLE001
            result["error"] = repr(error)[:200]

    sys.setrecursionlimit(200000)
    threading.stack_size(512 << 20)
    worker = threading.Thread(target=run)
    worker.start()
    worker.join()
    shutil.rmtree(overlay, ignore_errors=True)
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


def import_states(jobs, budget_s):
    """jobs: {key: (patched, test modules, masked bindings)}. Runs the fingerprint for each, plus
    the pristine sources once per set of test modules and masks, two at a time; returns {key:
    True if every digest matches the pristine one (and no import fails that didn't before)}."""
    import subprocess
    import tempfile
    import time

    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    runs = {}
    for key, (patched, mods, masks) in jobs.items():
        mods, masks = tuple(mods), tuple(sorted(masks))
        runs[key] = (patched, mods, masks)
        runs[("pristine", mods, masks)] = ({}, mods, masks)
    tmp = tempfile.mkdtemp(prefix="br-state-")
    pending = list(runs.items())
    running, outputs = {}, {}
    deadline = time.monotonic() + budget_s
    started = 0
    try:
        while pending or running:
            while pending and len(running) < PARALLEL and time.monotonic() < deadline:
                key, (patched, mods, masks) = pending.pop(0)
                started += 1
                path = os.path.join(tmp, f"job{started}.pkl")  # one file per job
                with open(path, "wb") as fh:
                    pickle.dump({"patched": patched, "masks": list(masks)}, fh)
                proc = subprocess.Popen([sys.executable, "-S", str(Path(__file__).resolve()), "--state", path, *mods],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=tmp,
                                        preexec_fn=_state_limits)
                running[key] = (proc, time.monotonic() + STATE_WALL_S)
            if not running:
                break
            time.sleep(0.02)
            for key, (proc, until) in list(running.items()):
                if proc.poll() is None and time.monotonic() < min(until, deadline):
                    continue
                if proc.poll() is None:
                    proc.kill()
                out = proc.communicate()[0]
                running.pop(key)
                try:
                    outputs[key] = json.loads(out.decode()).get("state")
                except ValueError:
                    outputs[key] = None
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    verdict = {}
    for key, (_, mods, masks) in jobs.items():
        mine, base = outputs.get(key), outputs.get(("pristine", tuple(mods), tuple(sorted(masks))))
        verdict[key] = bool(mine and base and mine["digests"] == base["digests"] and mine["failed"] == base["failed"])
    return verdict


IMPORT_CHECK = os.environ.get("BR_IMPORT_CHECK", "1") != "0"
IMPORT_WALL_S = 10.0


def import_failures(test_modules):
    """Import sqlglot, every sqlglot module, every dialect by name and the test modules whose
    import runs sqlglot code; the ones that raise (with the exception's type)."""
    import importlib
    import pkgutil

    failed = []
    try:
        import sqlglot
    except BaseException as error:  # noqa: BLE001
        return [f"sqlglot: {type(error).__name__}"]
    names = sorted(m.name for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot.") if not m.name.endswith("__main__"))
    for name in names:
        try:
            importlib.import_module(name)
        except BaseException as error:  # noqa: BLE001
            failed.append(f"{name}: {type(error).__name__}")
    try:
        from sqlglot.dialects.dialect import DIALECT_MODULE_NAMES, Dialect
        keys = sorted(DIALECT_MODULE_NAMES)
    except BaseException as error:  # noqa: BLE001
        failed.append(f"dialect registry: {type(error).__name__}")
        keys = []
    for key in keys:
        try:
            Dialect.get_or_raise(key)
        except BaseException as error:  # noqa: BLE001
            failed.append(f"dialect {key}: {type(error).__name__}")
    for name in sorted(test_modules):
        try:
            importlib.import_module(name)
        except BaseException as error:  # noqa: BLE001
            failed.append(f"{name}: {type(error).__name__}")
    return failed


def imports_main(args):
    """`selector.py --imports <patched pickle> [test modules]`: print which imports fail with
    that patch applied (an empty patch for the pinned sources)."""
    import shutil
    import site
    import threading

    path, test_modules = args[0], args[1:]
    with open(path, "rb") as fh:
        job = pickle.load(fh)
    site.main()
    overlay = enter_overlay(job["patched"])
    result = {}

    def run():
        try:
            result["failed"] = import_failures(test_modules)
        except BaseException as error:  # noqa: BLE001
            result["error"] = repr(error)[:200]

    sys.setrecursionlimit(200000)
    threading.stack_size(512 << 20)
    worker = threading.Thread(target=run)
    worker.start()
    worker.join()
    shutil.rmtree(overlay, ignore_errors=True)
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


def import_breaks(jobs, budget_s):
    """jobs: {commit: patched}. Whether, in a fresh interpreter, the patched sources make some
    import fail that doesn't fail at the pinned commit (or the run doesn't finish): such an
    edit breaks every test that imports what failed, which no line or attribute map sees
    (a dialect's import-time parse of its SQL templates, the lazy-import table). The pinned
    sources are only run when some commit's run reports a failure."""
    import subprocess
    import tempfile
    import time

    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    mods = tuple((load_imports() or {}).get("test_modules", ()))
    tmp = tempfile.mkdtemp(prefix="br-imports-")
    deadline = time.monotonic() + budget_s
    outputs = {}

    def run_all(items):
        pending, running, started = list(items), {}, 0
        while pending or running:
            while pending and len(running) < PARALLEL and time.monotonic() < deadline:
                key, patched = pending.pop(0)
                started += 1
                path = os.path.join(tmp, f"{len(outputs)}_{started}.pkl")
                with open(path, "wb") as fh:
                    pickle.dump({"patched": patched}, fh)
                proc = subprocess.Popen([sys.executable, "-S", str(Path(__file__).resolve()), "--imports", path, *mods],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=tmp,
                                        preexec_fn=_state_limits)
                running[key] = (proc, time.monotonic() + IMPORT_WALL_S)
            if not running:
                break
            time.sleep(0.01)
            for key, (proc, until) in list(running.items()):
                if proc.poll() is None and time.monotonic() < min(until, deadline):
                    continue
                if proc.poll() is None:
                    proc.kill()
                out = proc.communicate()[0]
                running.pop(key)
                try:
                    outputs[key] = json.loads(out.decode()).get("failed")
                except ValueError:
                    outputs[key] = None

    try:
        run_all(sorted(jobs.items()))
        if any(v for v in outputs.values()):
            run_all([("<pristine>", {})])
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    base = set(outputs.get("<pristine>") or ())
    verdict = {}
    for key in jobs:
        got = outputs.get(key)
        verdict[key] = got is None or bool(set(got) - base) or (bool(got) and outputs.get("<pristine>") is None)
        if verdict[key]:
            log(f"{key}: patched imports fail: {got if got is not None else 'no result'}")
    return verdict


def _record_import_order():
    """A meta-path hook recording, per module of ours, the ticks its import started and
    ended at (what refresh_modules needs to know which modules to load again)."""
    import importlib.abc
    import importlib.machinery
    import itertools

    tick = itertools.count()

    class Watch(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname.split(".")[0] not in _OURS:
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            loader = getattr(spec, "loader", None)
            if loader is None or not hasattr(loader, "exec_module"):
                return spec
            real = loader.exec_module

            def exec_module(module):
                start = next(tick)
                real(module)
                _IMPORT_ORDER[fullname] = (start, next(tick))

            loader.exec_module = exec_module
            return spec

    watch = Watch()
    sys.meta_path.insert(0, watch)
    return watch


def _zygote_imports(jobs, budget_s, wfd):
    """In a forked child of the selector: import the pinned sources once, then fork one
    grandchild per commit that reloads only what its patch reaches and imports everything
    again; writes {commit: failures or None} and the pinned run's failures."""
    import select
    import signal
    import time

    mods = tuple((load_imports() or {}).get("test_modules", ()))
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, str(SRC))
    os.chdir(str(SRC))
    watch = _record_import_order()
    base = import_failures(mods)
    sys.meta_path.remove(watch)
    out = {"base": base, "jobs": {}}
    deadline = time.monotonic() + budget_s
    pending, running = sorted(jobs.items()), {}
    while pending or running:
        while pending and len(running) < PARALLEL and time.monotonic() < deadline:
            key, patched = pending.pop(0)
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(r)
                    signal.signal(signal.SIGPROF, lambda *_: os._exit(3))
                    signal.setitimer(signal.ITIMER_PROF, 6.0)
                    enter_overlay(patched)
                    try:
                        refresh_modules(patched)
                        failed = []
                    except BaseException as error:  # noqa: BLE001  the patched import itself
                        failed = [f"refresh: {type(error).__name__}"]
                    failed += import_failures(mods)
                    os.write(w, json.dumps(failed).encode())
                    os._exit(0)
                except BaseException:
                    os._exit(1)
            os.close(w)
            running[pid] = (key, r, time.monotonic() + IMPORT_WALL_S, bytearray())
        if not running:
            break
        fds = {v[1]: pid for pid, v in running.items()}
        ready, _, _ = select.select(list(fds), [], [], 0.05)
        for fd in ready:
            pid = fds[fd]
            chunk = os.read(fd, 1 << 16)
            if chunk:
                running[pid][3].extend(chunk)
                continue
            key, _, _, buf = running.pop(pid)
            os.close(fd)
            _, status = os.waitpid(pid, 0)
            try:
                out["jobs"][key] = json.loads(buf.decode()) if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0 else None
            except ValueError:
                out["jobs"][key] = None
        now = time.monotonic()
        for pid, (key, fd, until, _) in list(running.items()):
            if now > until or now > deadline:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                os.waitpid(pid, 0)
                os.close(fd)
                running.pop(pid)
                out["jobs"][key] = None
    # commits with a new failure: which test modules no longer import (any of them, not just
    # those whose import runs sqlglot code). No pinned run: a test module that also fails
    # at the pinned commit only adds its tests.
    broken = sorted(k for k, v in out["jobs"].items() if v and set(v) - set(base) and k in IMPORT_DETAIL_FOR)
    if broken and IMPORT_TEST_MODULES and time.monotonic() < deadline:
        out["tests"] = _zygote_test_imports({k: jobs[k] for k in broken}, deadline)
    os.write(wfd, json.dumps(out).encode())


IMPORT_TEST_MODULES = []  # every test module, set by main for the import-failure narrowing
IMPORT_DETAIL_FOR = set()  # the commits that narrowing can use it for


def _zygote_test_imports(jobs, deadline):
    """In the import-check zygote: per commit, a grandchild that imports every test module
    on the patched sources; {commit: failing test modules, or None}."""
    import importlib
    import select
    import signal
    import site
    import time

    site.main()  # the test modules import third-party packages (setuptools, duckdb, pandas)
    for name in ("numpy", "pandas", "pandas.testing", "duckdb"):  # what the heavy test modules import
        try:
            importlib.import_module(name)
        except BaseException:  # noqa: BLE001
            pass
    res = {}
    pending, running = sorted(jobs.items()), {}
    while pending or running:
        while pending and len(running) < PARALLEL and time.monotonic() < deadline:
            key, patched = pending.pop(0)
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(r)
                    signal.signal(signal.SIGPROF, lambda *_: os._exit(3))
                    signal.setitimer(signal.ITIMER_PROF, 8.0)
                    enter_overlay(patched)
                    try:
                        refresh_modules(patched)
                    except BaseException:  # noqa: BLE001
                        pass
                    failed = []
                    for name in IMPORT_TEST_MODULES:
                        try:
                            importlib.import_module(name)
                        except BaseException as error:  # noqa: BLE001
                            failed.append(f"{name}: {type(error).__name__}")
                    os.write(w, json.dumps(failed).encode())
                    os._exit(0)
                except BaseException:
                    os._exit(1)
            os.close(w)
            running[pid] = (key, r, time.monotonic() + IMPORT_WALL_S, bytearray())
        if not running:
            break
        fds = {v[1]: pid for pid, v in running.items()}
        ready, _, _ = select.select(list(fds), [], [], 0.05)
        for fd in ready:
            pid = fds[fd]
            chunk = os.read(fd, 1 << 16)
            if chunk:
                running[pid][3].extend(chunk)
                continue
            key, _, _, buf = running.pop(pid)
            os.close(fd)
            _, status = os.waitpid(pid, 0)
            try:
                res[key] = json.loads(buf.decode()) if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0 else None
            except ValueError:
                res[key] = None
        now = time.monotonic()
        for pid, (key, fd, until, _) in list(running.items()):
            if now > until or now > deadline:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                os.waitpid(pid, 0)
                os.close(fd)
                running.pop(pid)
                res[key] = None
    return res


def import_breaks_forked(jobs, budget_s):
    """import_breaks, from a fork of this process that imports the pinned sources once and
    reloads per commit only the modules its patch reaches. None if that doesn't work out."""
    import select
    import signal
    import time

    r, w = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            if not os.environ.get("BR_DEBUG"):
                os.dup2(devnull, 2)
            _zygote_imports(jobs, budget_s, w)
            os._exit(0)
        except BaseException:
            os._exit(1)
    os.close(w)
    buf, until = bytearray(), time.monotonic() + budget_s + 10
    while time.monotonic() < until:
        ready, _, _ = select.select([r], [], [], 0.1)
        if ready:
            chunk = os.read(r, 1 << 16)
            if not chunk:
                break
            buf.extend(chunk)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    os.close(r)
    _, status, ru = os.wait4(pid, 0)
    log(f"import check of {len(jobs)} commits used {ru.ru_utime + ru.ru_stime:.2f}s CPU")
    try:
        out = json.loads(buf.decode())
    except ValueError:
        return None
    base = set(out["base"])
    verdict = {}
    tests = out.get("tests") or {}
    for key in jobs:
        got = out["jobs"].get(key)
        verdict[key] = got is None or bool(set(got) - base)
        if verdict[key]:
            log(f"{key}: patched imports fail: {got if got is not None else 'no result'}")
            extra = tests.get(key)
            if got is not None and extra is not None:
                IMPORT_DETAIL[key] = sorted(set(got) - base) + extra
                log(f"{key}: test modules failing to import: {extra}")
    return verdict


IMPORT_DETAIL = {}  # commit -> what fails to import with its patch (and every failing test module)


def needs_import_check(selector, snap, every):
    """A selection short of every test for a commit that edits sqlglot code at module or class
    level, or a function line that runs while modules are imported."""
    if len(snap["chosen"]) >= len(every) or not snap["patched"]:
        return False
    for rel, lo, hi, kind in snap["touched"]:
        if rel.startswith("sqlglot/") and rel.endswith(".py") and rel in snap["patched"]:
            if kind != "function" or selector.touches_import(rel, lo, hi):
                return True
    return False


def main():
    import time
    started = time.monotonic()
    tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
    duration = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
    every = sorted(duration)
    number_of = {t["id"]: t["number"] for t in tests}
    id_of = {t["number"]: t["id"] for t in tests}
    info = {}
    for t in tests:
        parts = t["id"].split("::")
        info[t["number"]] = {
            "file": t["file"],
            "runnable": (len(parts) == 3 and "[" not in t["id"] and t["file"] not in SKIP_FILES
                         and t["id"] not in SKIP_TESTS and duration[t["number"]] <= MAX_TEST_MS),
        }

    selector = None
    local_numbers, always = [], []
    try:
        with gzip.open(MAP, "rb") as fh:
            doc = pickle.load(fh)
        local_numbers = [number_of.get(test_id) for test_id in doc["tests"]]
        all_mask = (1 << len(local_numbers)) - 1
        selector = Selector(doc, all_mask)
        unknown = sorted(set(number_of) - set(doc["tests"]))
        always = [number_of[t] for t in unknown]
        if unknown:
            log(f"{len(unknown)} tests are not in the map; they run on every commit with a selection")
    except Exception as error:  # never let a map problem cost a commit
        log(f"map unavailable, running every test: {error!r}")

    def to_numbers(mask):
        out = []
        while mask:
            low = mask & -mask
            num = local_numbers[low.bit_length() - 1]
            if num is not None:
                out.append(num)
            mask ^= low
        return out

    def run_select(text, widen=True):
        """The selection for one patch, with what planning its checks needs; None on a fallback."""
        selector.no_import_widen = not widen
        try:
            mask = selector.select(text)
        finally:
            selector.no_import_widen = False
        chosen = to_numbers(mask)
        if chosen and always:
            chosen = sorted(set(chosen) | set(always))
        return {"chosen": chosen, "patched": dict(selector.patched), "verifiable": selector.verifiable,
                "unreadable": set(to_numbers(selector.data_mask)), "touched": list(selector.touched),
                "data_files": set(selector.data_files), "import_widened": selector.import_widened,
                "state_masks": list(selector.state_masks), "fixture_checks": dict(selector.fixture_checks),
                "env_keys": set(selector.env_keys),
                "cmp_filters": {k: list(v) for k, v in selector.cmp_filters.items()},
                "guard": to_numbers(selector.guard_mask) if selector.guard_mask else None}

    # 1. selections, and the commits whose selection an unchanged import-time state would shrink
    snaps, narrow, state_jobs = {}, {}, {}
    for patch in sorted((DATA / "commits").glob("*.patch")):
        commit = patch.name[: -len(".patch")]
        text = patch.read_bytes().decode("utf-8", "surrogateescape")
        try:
            if selector is None:
                raise Fallback("no map")
            snap = run_select(text)
        except Exception as error:
            log(f"{commit}: running every test ({error})")
            snap = {"chosen": every, "patched": {}, "verifiable": False, "unreadable": set(), "touched": [],
                    "data_files": set(), "import_widened": False, "state_masks": [], "fixture_checks": {},
                    "env_keys": set(), "cmp_filters": {}, "guard": None}
        snaps[commit] = snap
        if RUN and snap["verifiable"] and snap["import_widened"] and not snap["data_files"] and snap["patched"]:
            try:
                alt = run_select(text, widen=False)
            except Exception as error:
                log(f"{commit}: no narrower selection ({error})")
                alt = None
            gain = None
            if alt is not None:
                gain = pack_cost(snap["chosen"], duration) - pack_cost(alt["chosen"], duration)
                tpcds_number = number_of.get(TPCDS_TEST)
                if tpcds_number in alt["chosen"] and gain < STATE_MIN_GAIN_MS:
                    # as ordinary function code the commit may also get a TPC-DS replay
                    selector.patched, selector.verifiable = alt["patched"], alt["verifiable"]
                    selector.touched, selector.data_files = alt["touched"], alt["data_files"]
                    selector.env_keys = alt["env_keys"]
                    selector.cmp_filters = alt["cmp_filters"]
                    if selector.tpcds_plan(alt["chosen"], duration, tpcds_number):
                        gain += (pack_cost(alt["chosen"], duration)
                                 - pack_cost([n for n in alt["chosen"] if n != tpcds_number], duration)) // 2
            if gain is not None and gain >= STATE_MIN_GAIN_MS:
                narrow[commit] = alt
                state_jobs[commit] = (snap["patched"], tuple((load_imports() or {}).get("test_modules", ())),
                                      tuple(alt["state_masks"]))
    if state_jobs:
        verdict = import_states(state_jobs, max(5.0, STATE_BUDGET_S))
        for commit, same in sorted(verdict.items()):
            log(f"{commit}: import-time state {'unchanged' if same else 'changed or unknown'}")
            if same:
                snaps[commit] = narrow[commit]

    # 1a. commits that select every test only for an import edit or a lost name read
    # elsewhere: if that module then fails to import, its importers are what can fail
    deferred = {}
    if IMPORT_CHECK and ZYGOTE and selector is not None:
        for commit, snap in snaps.items():
            if len(snap["chosen"]) < len(every) or not snap["patched"]:
                continue
            text = (DATA / "commits" / f"{commit}.patch").read_bytes().decode("utf-8", "surrogateescape")
            selector.defer_all = True
            try:
                alt = run_select(text)
                rels = set(selector.deferred)
            except Exception:
                alt, rels = None, set()
            finally:
                selector.defer_all = False
            if alt is not None and rels and len(alt["chosen"]) < len(every):
                deferred[commit] = (alt, rels)
        IMPORT_DETAIL_FOR.update(deferred)
        IMPORT_TEST_MODULES[:] = sorted(f[:-3].replace("/", ".") for f in selector.doc["test_files"]
                                        if f.endswith(".py") and not f.endswith("__init__.py"))

    # 1b. edits that run while modules are imported: does every import still succeed?
    if IMPORT_CHECK and selector is not None:
        imp_jobs = {c: snap["patched"] for c, snap in snaps.items()
                    if needs_import_check(selector, snap, every) or c in deferred}
        if imp_jobs:
            verdict = import_breaks_forked(imp_jobs, 30.0) if ZYGOTE else None
            if verdict is None:
                verdict = import_breaks(imp_jobs, 30.0)
            for commit, broken in verdict.items():
                if commit in deferred:
                    alt, rels = deferred[commit]
                    detail = IMPORT_DETAIL.get(commit) if broken else None
                    mods = {e.rpartition(": ")[0] for e in detail or ()}
                    extra = selector.import_failure_mask(detail) if detail else None
                    # only when every file the commit changes is a module that no longer imports:
                    # nothing of theirs runs, and what the rest of the map recorded still holds
                    files = set(alt["patched"]) | rels
                    if extra is not None and all(r.startswith("sqlglot/") and r.endswith(".py") and
                                                 r[:-3].replace("/", ".").removesuffix(".__init__") in mods
                                                 for r in files):
                        chosen = sorted(set(alt["chosen"]) | set(to_numbers(extra)) | set(always))
                        log(f"{commit}: {', '.join(sorted(rels))} no longer imports: {len(chosen)} tests")
                        snaps[commit] = dict(alt, chosen=chosen, verifiable=False)
                    continue
                if broken:
                    snaps[commit] = dict(snaps[commit], chosen=every, verifiable=False)

    # 2. the checks each selection is worth
    answers = {}
    jobs, tpcds_jobs = [], []
    guards = {}  # commit -> the selection it falls back to unless its TPC-DS child imports the patch
    tpcds_number = number_of.get(TPCDS_TEST)
    genmem_number = number_of.get(GENMEM_TEST)
    for commit, snap in snaps.items():
        chosen, patched, verifiable, unreadable = snap["chosen"], snap["patched"], snap["verifiable"], snap["unreadable"]
        if selector is not None:
            selector.patched, selector.verifiable = patched, verifiable
            selector.touched, selector.data_files = snap["touched"], snap["data_files"]
            selector.env_keys = snap["env_keys"]
            selector.cmp_filters = snap.get("cmp_filters", {})
        if snap["guard"] is not None:
            wide = sorted(set(chosen) | set(snap["guard"]))
            if tpcds_number not in chosen:
                chosen = wide  # (the narrowed selection always has it: it reads ENV)
            guards[commit] = wide
        answers[commit] = chosen
        if RUN and verifiable and selector is not None and tpcds_number in chosen:
            plan = selector.tpcds_plan(chosen, duration, tpcds_number)
            if not plan and commit in guards:
                # too dear to replay: the child only imports the patched sources and keeps the test
                plan = {"mode": "full", "modes": {}, "queries": [], "estimate": 1.0, "budget": 10.0, "keep": True}
            if plan:
                plan["commit"] = commit
                plan["guard"] = commit in guards
                tpcds_jobs.append(((commit, "tpcds"), patched, [TPCDS_TEST],
                                   {"__tpcds__": plan, TPCDS_TEST: duration[tpcds_number] / 1000},
                                   10 + max(3 * plan["estimate"], 1.5 * plan["budget"])))
        if commit in guards and not any(job[0] == (commit, "tpcds") for job in tpcds_jobs):
            answers[commit] = chosen = guards.pop(commit)  # nothing will import the patch
        if RUN and verifiable and selector is not None and snap.get("fixture_checks"):
            fplan = fixture_plan(snap["fixture_checks"], chosen, duration, number_of)
            if fplan:
                tpcds_jobs.append(((commit, "fixture"), patched, sorted(fplan["tests"]),
                                   {"__fixture__": fplan, **{t: duration[number_of[t]] / 1000 for t in fplan["tests"]}},
                                   10 + 1.5 * fplan["budget"]))
        if RUN and verifiable and selector is not None and genmem_number in chosen:
            gplan = selector.genmem_plan(chosen, duration, genmem_number)
            if gplan is not None and not gplan["dialects"] and not gplan["import_check"]:
                chosen = [n for n in chosen if n != genmem_number]  # no dialect loads what changed
                answers[commit] = chosen
            elif gplan is not None:
                tpcds_jobs.append(((commit, "genmem"), patched, [GENMEM_TEST],
                                   {"__genmem__": gplan, GENMEM_TEST: duration[genmem_number] / 1000},
                                   10 + 3 * gplan["estimate"]))
        if RUN and verifiable and chosen:
            check = plan_check(chosen, duration, info, unreadable)
            if check:
                expected = sum(duration[n] for n in check) / 1000
                limit = 10 + 4 * expected
                jobs.append((commit, patched, [id_of[n] for n in check],
                             {id_of[n]: duration[n] / 1000 for n in check}, limit))
        log(f"{commit}: {len(chosen)} tests")

    jobs = tpcds_jobs + jobs
    if jobs:
        budget = float(os.environ.get("BR_BUDGET", "140")) - (time.monotonic() - started)
        warm(jobs)
        preimport(jobs)
        # every check is a fork of this process: keep the collector in each child away from
        # the maps and imports already here (walking them would also copy their pages)
        import gc
        gc.freeze()
        results = run_checks(jobs, budget)
        for commit in list(guards):
            if results.get((commit, "tpcds")) is None:  # the patched sources didn't import, or no verdict
                answers[commit] = guards.pop(commit)
        for key, _, test_ids, _, _ in jobs:
            commit = key[0] if isinstance(key, tuple) else key
            failed = results.get(key)
            if failed is None:
                continue
            passed = {number_of[t] for t in test_ids if t not in failed}
            log(f"{commit}: ran {len(test_ids)}, {len(test_ids) - len(passed)} failed")
            answers[commit] = [n for n in answers[commit] if n not in passed]

    out = [json.dumps({"commit": c, "workers": pack(answers[c], duration)}, separators=(",", ":"))
           for c in answers]
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--state":
        state_main(sys.argv[2:])
    elif len(sys.argv) > 2 and sys.argv[1] == "--imports":
        imports_main(sys.argv[2:])
    else:
        main()
