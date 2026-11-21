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
- A changed fixture or data file selects the tests that opened it.
- Anything we can't reason about (a patch that won't apply, a syntax error, a
  module-level change other modules import, config files) runs every test.

Logs go to stderr. stdout holds exactly one JSON line per commit.
"""

import ast
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
MAP = Path(os.environ.get("BR_MAP", str(Path(__file__).with_name("blast_map.pkl.gz"))))
TPCDS_DATA = Path(os.environ.get("BR_TPCDS", str(Path(__file__).with_name("tpcds.pkl.gz"))))
GLOBALS_DATA = Path(os.environ.get("BR_GLOBALS", str(Path(__file__).with_name("globals_refs.pkl.gz"))))
GENMEM_DATA = Path(os.environ.get("BR_GENMEM", str(Path(__file__).with_name("genmem.pkl.gz"))))
GENMEM_TEST = "tests/test_generator.py::TestGenerator::test_generator_memory"
TPCDS_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
# Plan a TPC-DS replay only if its estimated CPU is at most this share of the time it would
# save. Full replays run the executor on changed code, where outputs often differ late and
# changed plans can run slower, so they get a stricter gate.
TPCDS_GATE = {"front": 0.8, "full": 0.5}
TPCDS_BUDGET = 0.85  # the replay child is stopped (and the test kept) past this share of the saving
TPCDS_RERUN_SHARE = 0.5  # second front pass only if its estimate fits in half of what's left
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
        d = {"old": None, "new": None, "hunks": [], "binary": False, "new_file": False, "deleted": False}
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
        self.string_lines = set()
        self.continued = set()
        for idx, line in enumerate(self.lines, start=1):
            if line.rstrip().endswith("\\"):
                self.continued.add(idx)
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.Constant, ast.JoinedStr)) and isinstance(getattr(node, "value", ""), (str, bytes, list)):
                end = getattr(node, "end_lineno", node.lineno)
                if end and end > node.lineno:
                    self.string_lines.update(range(node.lineno + 1, end + 1))
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
                return self.ALL
            names = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    names.add(sub.id)
            if isinstance(node, DEFS + (ast.ClassDef,)):
                names.add(node.name)
            if names & set(doc["exported"].get(rel, ())):
                return self.ALL
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
            m = self.line_union(rel, lo, hi)
            if self.touches_import(rel, lo, hi):
                func = st["func"]
                m |= self.broad_for_function(rel, idx, func)
                self.touched.append((rel, lo, hi, "other"))
            else:
                self.touched.append((rel, lo, hi, "function"))
            return m
        self.touched.append((rel, lo, hi, "other"))
        if st["kind"] == "def":
            body_lo, body_hi = st["full"]
            m = self.line_union(rel, body_lo, body_hi)
            in_decorators = line < st["node"].lineno
            if in_decorators and st["scope"] == "class":
                m |= self.class_fallback(rel, st["cls"], idx)
            elif in_decorators:
                m |= self.module_fallback(rel, idx)
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
        return self.module_fallback(rel, idx, st["node"])

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
        if len(blk["removed"]) != 1 or len(blk["added"]) != 1:
            return mask
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if targets and all(isinstance(t, ast.Name) for t in targets):
            reads = [self.doc["attr_read_masks"].get(t.id) for t in targets]
            if all(r is not None for r in reads):
                rm = 0
                for r in reads:
                    rm |= r
                mask = rm if mask is None else mask & rm
        return mask

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
        m = 0
        for blk in blocks:
            if self.comment_only(blk, idx, new_idx):
                continue
            for ln in blk["removed"]:
                m |= self.mask_for_old_line(rel, idx, ln, blk)
            if blk["added"]:
                m |= self.mask_for_added(rel, idx, new_idx, blk)
        return m

    def mask_for_added(self, rel, idx, new_idx, blk):
        """Added lines inside a function run where the lines around them ran. A new or
        rewritten class attribute, method or class changes the class; a new module
        statement changes the module."""
        old_defs = {(st["cls"], st["node"].name) for st in idx.stmts if st["kind"] in ("def", "class")}
        same_shape = len(blk["removed"]) == len(blk["added"])
        for ln in blk["added"]:
            st, _ = new_idx.stmt_at(ln)
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
        if not blk["removed"]:
            anchor = blk["anchor"]
            for ln in (anchor - 1, anchor):
                if 0 < ln <= len(idx.lines):
                    m |= self.mask_for_old_line(rel, idx, ln)
        return m

    @staticmethod
    def comment_only(blk, idx, new_idx):
        if not all(is_comment_or_blank(t) for t in blk["removed_text"] + blk["added_text"]):
            return False
        if any(ln in idx.string_lines for ln in blk["removed"]):
            return False
        if any(ln in new_idx.string_lines for ln in blk["added"]):
            return False
        anchor = blk["anchor"]
        if (anchor - 1) in idx.continued:
            return False
        return True

    def test_source(self, rel, fd):
        doc = self.doc
        file_mask = doc["test_files"].get(rel, 0)
        try:
            old_text, idx = self.index(rel)
        except (OSError, SyntaxError):
            self.verifiable = False
            return file_mask
        new_text, blocks = apply_hunks(old_text, fd["hunks"])
        self.patched[rel] = new_text
        try:
            new_idx = SourceIndex(new_text)
        except SyntaxError:
            return file_mask
        m = 0
        for blk in blocks:
            if self.comment_only(blk, idx, new_idx):
                continue
            lines = blk["removed"] or [blk["anchor"] - 1, blk["anchor"]]
            for ln in lines:
                st, _ = idx.stmt_at(ln)
                if st is None or st["scope"] == "module" and st["kind"] != "class":
                    return file_mask
                cls = st["cls"]
                func = st["func"] if st["scope"] == "function" else (st["node"] if st["kind"] == "def" else None)
                if cls and func is not None:
                    test_id = f"{rel}::{cls}::{func.name}"
                    tm = self.test_bit.get(test_id)
                    m |= tm if tm else doc["test_classes"].get(f"{rel}::{cls}", file_mask)
                elif cls:
                    m |= doc["test_classes"].get(f"{rel}::{cls}", file_mask)
                else:
                    return file_mask
        return m

    def select(self, text):
        doc = self.doc
        m = 0
        self.patched = {}
        self.verifiable = True
        self.touched = []
        diffs = parse_patch(text)
        if not diffs:
            raise Fallback("no file changes found in the patch")
        for fd in diffs:
            rel = fd["new"] or fd["old"]
            old_rel = fd["old"]
            if rel is None:
                continue
            if fd["binary"] or fd["deleted"] or (old_rel and old_rel != rel):
                raise Fallback(f"binary, deleted or renamed file {rel}")
            m |= doc["file_opens"].get(rel, 0)  # tests that read this file's text
            if rel in self.config or os.path.basename(rel) == "conftest.py":
                raise Fallback(f"config file {rel}")
            if not rel.endswith(".py") or not rel.startswith(("sqlglot/", "tests/")):
                self.verifiable = False  # data files and the like: we only overlay Python sources
            if fd["new_file"]:
                self.verifiable = False
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
        return m

    # ---- the 21 s TPC-DS executor test: replay what the edit can reach instead of running it

    def tpcds_plan(self, chosen, duration, tpcds_number):
        """A replay plan when it is provably enough and clearly cheaper than the test, else None.

        Only for commits whose every change is ordinary function code in sqlglot/ (no class
        or module level, nothing run at import, no data or test files), none of which runs
        in the test's table setup. "front" (compare the optimizer output) only when no
        changed line runs anywhere but inside optimize() for the reached queries; else
        "full" (compare the executor result). Both also compare the duckdb transpilation,
        which is the expected side of the test's comparison."""
        if not self.verifiable or not self.patched or not self.touched:
            return None
        if any(kind != "function" or not rel.startswith("sqlglot/") for rel, _, _, kind in self.touched):
            return None
        data = load_tpcds()
        if data is None:
            return None

        def hits(sets, rel, lo, hi):
            lines = sets.get(rel)
            return bool(lines) and any(ln in lines for ln in range(lo, hi + 1))

        spans = [(rel, lo, hi) for rel, lo, hi, _ in self.touched]
        if any(hits(data["setup_sets"], *s) for s in spans):
            return None
        reached = [q for q in data["queries"] if any(hits(q["opt_sets"], *s) or hits(q["rest_sets"], *s) for s in spans)]
        if not reached:
            return None  # the test runs this code somewhere the replay doesn't: keep it
        front = not any(hits(data["digest_sets"], *s) for s in spans) and \
            not any(hits(q["rest_sets"], *s) for q in reached for s in spans)
        key = "front_cpu" if front else "cpu"
        estimate = sum(q[key] for q in reached) + 1.0
        saving = (pack_cost(chosen, duration) - pack_cost([n for n in chosen if n != tpcds_number], duration)) / 1000
        if estimate > TPCDS_GATE["front" if front else "full"] * saving:
            return None
        reached.sort(key=lambda q: q[key])  # cheapest first: a changed output usually shows early
        # The child stops and keeps the test once it has used this much CPU. In front mode
        # it also re-runs to the end the queries whose optimizer output changed, if that
        # still fits, since a different plan often gives the same rows.
        budget = TPCDS_BUDGET * saving
        log(f"TPC-DS replay planned: {'front' if front else 'full'}, {len(reached)} queries, "
            f"est {estimate:.1f}s to save {saving:.1f}s, budget {budget:.1f}s")
        return {"mode": "front" if front else "full", "queries": [q["index"] for q in reached],
                "estimate": estimate, "budget": budget}

    # ---- test_generator_memory: rerun its per-dialect check only for dialects the edit can reach

    def genmem_plan(self, chosen, duration, genmem_number):
        """Which dialects to recheck, or None to keep the test as is.

        The test builds every dialect's generator 1,100 times and checks that the last 1,000
        builds leave under 1 KB behind. Only sqlglot modules that are loaded when a dialect's
        generator is built can change that dialect's result (tools/build_genmem.py records
        them per dialect, from a fresh interpreter). Only for commits that change nothing but
        sqlglot/ Python files."""
        if not self.verifiable or not self.patched:
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


def plan_check(chosen, duration, info):
    """The cheapest-first prefix of runnable tests whose check is expected to pay off."""
    runnable = sorted((n for n in chosen if info[n]["runnable"]), key=lambda n: (duration[n], n))
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
            for q in data["queries"]:
                q["opt_sets"] = {r: set(v) for r, v in q["opt_lines"].items()}
                q["rest_sets"] = {r: set(v) for r, v in q["rest_lines"].items()}
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

    Front mode (changed code runs only inside optimize() for these queries): a first pass
    stops each query right after optimize() and compares a digest of its output. Queries
    whose optimizer output is unchanged are settled, since nothing after optimize() runs
    changed code. If some changed, a second pass runs just those to the end, reusing the
    first pass's optimizer output, and compares their rows, provided that fits the budget.
    Full mode runs every planned query to the end. Either way the child gives up (and the
    test stays selected) once its own CPU passes the plan's budget."""
    import hashlib
    import shutil
    import signal
    import time

    budget = plan.get("budget", float("inf"))
    if budget != float("inf"):
        # a hard stop: a changed plan can make one query run far longer than recorded
        signal.signal(signal.SIGPROF, lambda *_: os._exit(3))
        signal.setitimer(signal.ITIMER_PROF, budget)
    overlay = enter_overlay(patched)
    data = load_tpcds()
    from sqlglot import find_tables, parse_one, transpile
    import sqlglot.executor as executor_module
    from sqlglot.executor import execute
    from sqlglot.executor.table import Table

    tables = {t: Table(columns=c, rows=data["tables"][t]) for t, c in data["schema"].items()}
    schema, tables = pickle.loads(pickle.dumps((data["schema"], tables)))
    by_index = {q["index"]: q for q in data["queries"]}
    front = plan["mode"] == "front"
    debug = bool(os.environ.get("BR_DEBUG"))
    original_optimize = executor_module.optimize
    current = {}
    optimized = {}  # first-pass optimizer output of the queries whose digest changed

    class Stop(BaseException):
        pass

    def optimize_hook(*args, **kwargs):
        q = current["q"]
        if q["index"] in optimized:  # second pass: carry on from the first pass's output
            return optimized.pop(q["index"])
        result = original_optimize(*args, **kwargs)
        current["digest"] = hashlib.sha256(repr(result.dump()).encode()).hexdigest()
        if current["digest"] != q["opt_digest"]:
            optimized[q["index"]] = result
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
            return None
        return tuple(result.columns) == q["columns"] and list(result.rows) == q["rows"]

    same = True
    early = 0
    if front:
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
        if debug:
            print(f"tpcds child: first pass same={same} spent={spent():.2f}s changed_plans={len(optimized)} "
                  f"budget={budget:.2f}s", file=sys.stderr)
        if same and optimized:
            todo = sorted((by_index[i] for i in optimized), key=lambda q: q["cpu"] - q["front_cpu"])
            rerun = sum(max(q["cpu"] - q["front_cpu"], 0.0) for q in todo)
            if rerun > TPCDS_RERUN_SHARE * (budget - spent()):
                same = False  # too many changed plans to afford, with room for them running slower
            for q in todo if same else ():
                if run(q) is not True or spent() > budget:
                    if debug:
                        print(f"tpcds child: query {q['index']} differs or over budget, spent={spent():.2f}s",
                              file=sys.stderr)
                    same = False
                    break
    finally:
        executor_module.optimize = original_optimize
    if debug:
        print(f"tpcds child: same={same} spent={spent():.2f}s", file=sys.stderr)
    shutil.rmtree(overlay, ignore_errors=True)
    os.write(wfd, json.dumps([] if same else [TPCDS_TEST]).encode())
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

    overlay = enter_overlay(patched)
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
           if "__tpcds__" not in job[3] and "__genmem__" not in job[3] for t in job[2]):
        names += ["numpy", "pandas", "pandas.testing", "duckdb"]
    for name in names:
        try:
            importlib.import_module(name)
        except Exception:
            pass


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
                        child_tpcds(patched, limits["__tpcds__"], w)
                    elif "__genmem__" in limits:
                        child_genmem(patched, limits["__genmem__"], w)
                    else:
                        child_run(patched, test_ids, limits, w)
                    os._exit(0)
                except BaseException:
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

    answers = {}
    jobs, tpcds_jobs = [], []
    tpcds_number = number_of.get(TPCDS_TEST)
    genmem_number = number_of.get(GENMEM_TEST)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        commit = patch.name[: -len(".patch")]
        patched, verifiable = {}, False
        try:
            if selector is None:
                raise Fallback("no map")
            mask = selector.select(patch.read_bytes().decode("utf-8", "surrogateescape"))
            patched, verifiable = dict(selector.patched), selector.verifiable
            chosen = []
            while mask:
                low = mask & -mask
                num = local_numbers[low.bit_length() - 1]
                if num is not None:
                    chosen.append(num)
                mask ^= low
            if chosen and always:
                chosen = sorted(set(chosen) | set(always))
        except Exception as error:
            log(f"{commit}: running every test ({error})")
            chosen = every
        answers[commit] = chosen
        if RUN and verifiable and selector is not None and tpcds_number in chosen:
            plan = selector.tpcds_plan(chosen, duration, tpcds_number)
            if plan:
                tpcds_jobs.append(((commit, "tpcds"), patched, [TPCDS_TEST],
                                   {"__tpcds__": plan, TPCDS_TEST: duration[tpcds_number] / 1000},
                                   10 + 3 * plan["estimate"]))
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
            check = plan_check(chosen, duration, info)
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
        results = run_checks(jobs, budget)
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
    main()
