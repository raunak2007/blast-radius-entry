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
FIXTURES_DATA = Path(os.environ.get("BR_FIXTURES", str(Path(__file__).with_name("fixtures.pkl.gz"))))
CMP_NAMES = {ast.Is: "is", ast.IsNot: "is not", ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=",
             ast.Gt: ">", ast.GtE: ">=", ast.In: "in", ast.NotIn: "not in"}
CMP_ALT = {"is": "==", "is not": "!=", "<": "<=", "<=": "<", ">": ">=", ">=": ">"}  # as tools/cmp_plugin.py
GENMEM_TEST = "tests/test_generator.py::TestGenerator::test_generator_memory"
# file -> (function that builds a per-process cache on first use, the cache's name)
FIRST_USE = {"sqlglot/generator.py": ("_build_dispatch", "_DISPATCH_CACHE")}
TPCDS_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
# test_max_nodes (28.8 s, the slowest test) parses 10,000 copies of a depth-100 comparison
# against node limits of 1,000,000 and 50,000. At the pinned commit the statement makes
# 1,000,001 counted nodes, so the first limit trips on its very last node. A child
# (child_maxnodes) runs the test's small asserts as written and the two limit asserts on
# prefixes of the statement: the patched parser's node count must be exactly linear in the
# number of copies (10, 40 and 160 of them) and put 10,000 copies past 1,000,000; a limit one
# below a prefix's count must trip on that prefix's last node with the error the test looks
# for, and a prefix long enough to pass 50,000 nodes must raise it mid-statement.
MAXNODES_TEST = "tests/test_parser.py::TestParser::test_max_nodes"
MAXNODES_CPU_S = 1.5  # the child's CPU: the test module's imports and about 85,000 counted nodes
MAXNODES_BUDGET_S = 4.0  # past this much CPU the child stops and the test stays (an edit that slows parsing)
MAXNODES_P_PASS = 0.9  # how often the test passes where the check is planned
LAZY_LOAD_TEST = "tests/dialects/test_dialect.py::TestDialect::test_lazy_load"
# Plan a TPC-DS replay only if its estimated CPU is at most this share of the time it would
# save. Full replays run the executor on changed code, where outputs often differ late and
# changed plans can run slower, so they get a stricter gate.
TPCDS_GATE = {"front": 0.8, "full": float(os.environ.get("BR_TPCDS_GATE_FULL", "0.66"))}
TPCDS_BUDGET = 0.85  # the replay child is stopped (and the test kept) past this share of the saving
TPCDS_RERUN_SHARE = float(os.environ.get("BR_RERUN_SHARE", "0.5"))  # second front pass only if its estimate fits in this share of what is left
TPCDS_EARLY_RERUNS = 3  # changed plans run to the end at once, before deferring the rest
# The replay child also stops once its CPU passes this allowance plus this many times the
# recorded CPU of the work it has started: an edit that makes a plan explode (a cross join
# where there was a filter) used to burn the whole budget before failing.
# A normal replay uses about 0.3 s to set up and 0.7 times the recorded CPU here, so this
# leaves room for a machine several times slower.
TPCDS_SETUP_S = float(os.environ.get("BR_TPCDS_SETUP", "3.0"))
TPCDS_PACE = float(os.environ.get("BR_TPCDS_PACE", "6.0"))
WORKER_MS = 2000
MAX_WORKERS = 8
# Lambdas whose body ran while sqlglot's modules and the test modules were imported (or the
# executor's tables built, the rest of what the TPC-DS replay skips as setup), at the pinned
# commit: file -> their first lines (tools/build_import_lambdas.py). The DuckDB generator and
# Hive's type annotations parse SQL templates when they are imported, which runs these entries
# of the parser's lookup tables. An edit inside one can change what those imports build, so
# it is never narrowed to the tests that ran the lambda.
IMPORT_RUN_LAMBDAS = {
    "sqlglot/errors.py": frozenset((139,)),
    "sqlglot/expressions/core.py": frozenset((1921,)),
    "sqlglot/helper.py": frozenset((155,)),
    "sqlglot/parser.py": frozenset((
        419, 420, 444, 1094, 1122, 1162, 1228, 1230, 1246, 1273, 1282, 1283, 1284, 1286, 1292, 1301, 1306, 1586,
        1611, 1618, 1621, 1631, 1643, 1644, 4870, 5568, 5815, 6930, 6989, 7593, 7881, 8390, 8827, 9379, 10520,
    )),
}

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
            elif line.startswith(("Binary files", "GIT binary patch")):
                cur["binary"] = True
            elif line.startswith(("old mode ", "new mode ")):
                cur["mode"] = True
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
                cur = {"removed": [], "added": [], "anchor": o, "new_anchor": nn, "removed_text": [], "added_text": []}
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


IMPORTABLE = (".py", ".pyi", ".pyc", ".so", ".pyd", ".pth")
# pyproject.toml at the pinned commit (the image holds only sqlglot/ and tests/), to apply a
# patch to it
PINNED_PYPROJECT = """[project]
name = "sqlglot"
dynamic = ["version", "optional-dependencies"]
description = "An easily customizable SQL parser and transpiler"
readme = "README.md"
authors = [{ name = "Toby Mao", email = "toby.mao@gmail.com" }]
license = "MIT"
license-files = ["LICENSE"]
requires-python = ">= 3.9"
classifiers = [
    "Development Status :: 5 - Production/Stable",
    "Intended Audience :: Developers",
    "Intended Audience :: Science/Research",
    "Operating System :: OS Independent",
    "Programming Language :: SQL",
    "Programming Language :: Python :: 3 :: Only",
]

[project.urls]
Homepage = "https://sqlglot.com/"
Documentation = "https://sqlglot.com/sqlglot.html"
Repository = "https://github.com/tobymao/sqlglot"
Issues = "https://github.com/tobymao/sqlglot/issues"

[build-system]
requires = ["setuptools >= 61.0", "setuptools_scm >= 10.2"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
include-package-data = false

[tool.setuptools_scm]
version_file = "sqlglot/_version.py"
write_to_source = true
fallback_version = "0.0.0"
local_scheme = "no-local-version"

[tool.setuptools.packages.find]
include = ["sqlglot", "sqlglot.*"]

[tool.setuptools.package-data]
"*" = ["py.typed"]

[tool.uv.sources]
sqlglotc = { path = "./sqlglotc" }

[tool.ruff]
line-length = 100

[tool.ruff.lint]
extend-select = ["UP"]
ignore = ["E721", "E741"]
"""


# setup.cfg at the pinned commit, for the same reason
PINNED_SETUP_CFG = """[mypy]
disallow_untyped_calls = False
no_implicit_optional = True

[mypy-sqlglot.*]
ignore_errors = False

[mypy-sqlglot.dataframe.*]
ignore_errors = False

[mypy-tests.*]
ignore_errors = True

[mypy-tests.dataframe.*]
ignore_errors = False
"""


def setup_cfg_inert(hunks):
    """Whether an edit to setup.cfg leaves a pytest run from the checkout as it was. pytest parses
    the file on every run with iniconfig (the parser in the labeling environment's lock file,
    used here too) and stops on one it can't parse; of its content it uses the [tool:pytest]
    section only, and it refuses a [pytest] one. The pinned file has neither; no test opens it."""
    try:
        new_text, _ = apply_hunks(PINNED_SETUP_CFG, hunks)
        new_text.encode("utf-8")  # bytes that aren't UTF-8 arrive as surrogates
        if not _SITE_LOADED:
            import site

            site.main()  # the installed packages (the selector itself starts without them)
            _SITE_LOADED.append(True)
        import iniconfig

        # as a file opened in text mode hands it over
        data = new_text.replace("\r\n", "\n").replace("\r", "\n")
        sections = iniconfig.IniConfig("setup.cfg", data=data).sections
    except Exception:  # doesn't apply, not UTF-8, doesn't parse, no iniconfig
        return False
    return "tool:pytest" not in sections and "pytest" not in sections


def pyproject_inert(hunks):
    """Whether an edit to pyproject.toml leaves a pytest run from the checkout as it was (the
    commits were labeled with the checkout on PYTHONPATH and nothing installed from it). pytest
    reads the file as UTF-8 and TOML and stops on one it can't parse; of its content it uses the
    [tool.pytest] tables only, which the pinned file doesn't have; no test opens the file."""
    try:
        import tomllib
        new_text, _ = apply_hunks(PINNED_PYPROJECT, hunks)
        new_text.encode("utf-8")  # bytes that aren't UTF-8 arrive as surrogates
        tool = tomllib.loads(new_text).get("tool", {})
    except Exception:  # doesn't apply, not UTF-8, not TOML, no tomllib
        return False
    return isinstance(tool, dict) and "pytest" not in tool
# folders whose Python no test imports and no build of the package reads
UNBUILT_DIRS = ("benchmarks/", "docs/", "posts/", "pdoc/", ".github/")
# The two TPC query files and the tests that use what they hold (the rest of TestExecutor only
# loads them in setUpClass): "cases" read whole (meta, input, expected) cases, "inputs" only
# the meta lines and the input query. From tests/test_executor.py and tests/test_optimizer.py.
TPC_QUERY_FILES = {
    "tests/fixtures/optimizer/tpc-h/tpc-h.sql": {
        "cases": ("tests/test_optimizer.py::TestOptimizer::test_tpch",
                  "tests/test_executor.py::TestExecutor::test_optimized_tpch"),
        "inputs": ("tests/test_executor.py::TestExecutor::test_execute_tpch",)},
    "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql": {
        "cases": ("tests/test_optimizer.py::TestOptimizer::test_tpcds",),
        "inputs": ("tests/test_executor.py::TestExecutor::test_execute_tpcds",)},
}


def fixture_pairs(text):
    """The (meta, input, expected) cases of a fixture text, split exactly as the tests' own
    load_sql_fixture_pairs and _extract_meta (tests/helpers.py) split the file; raises where
    they would."""
    body = "\n".join(line for line in text.splitlines() if line and not line.startswith("--"))
    statements = body.split(";")
    out = []
    for i in range(0, len(statements), 2):
        if i + 1 < len(statements):
            lines = statements[i].strip().split("\n")
            meta, j = {}, 0
            while lines[j].startswith("#"):
                key, val = lines[j].split(":", maxsplit=1)
                meta[key.lstrip("#").strip()] = val.strip()
                j += 1
            out.append((meta, "\n".join(lines[j:]), statements[i + 1].strip()))
    return out
# files pytest reads its settings from when they sit at the repository root
PYTEST_CONFIGS = frozenset(("pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg",
                            "pyproject.toml"))
_SITE_LOADED = []


def asked_for_by_name(top, stem):
    """Whether a source at the pinned commit may import a module of this name, which exists for
    it only once a new file does: sqlglot/__init__.py tries sqlglot._version and goes on without
    it, and dialects load by name on demand. For a file under sqlglot/ that is the name as a
    whole word anywhere in sqlglot's sources or the tests (a string names a dialect); for one
    under tests/, in an import statement of a test file (test methods share names with files)."""
    word = re.compile(r"\b" + re.escape(stem) + r"\b")
    for folder in ("sqlglot", "tests") if top == "sqlglot" else ("tests",):
        for path in (SRC / folder).rglob("*.py"):
            try:
                text = path.read_text("utf-8", "surrogateescape")
            except OSError:
                return True
            if top == "sqlglot":
                if word.search(text):
                    return True
            elif any(word.search(ln) for ln in text.split("\n") if ln.lstrip().startswith(("import ", "from "))):
                return True
    return False


def new_module_risk(rel):
    """Why a new importable file can change tests although nothing at the pinned commit imports
    it, or None. The tests run with the repository root first on sys.path, so a new top-level
    module or package hides a standard-library or installed one of the same name (a root
    enum.py breaks everything). A new package directory hides the module file of the same
    name beside it, and a new __init__.py changes what pytest takes for a package."""
    parts = rel.split("/")
    base = parts[-1]
    if base in ("__init__.py", "sitecustomize.py", "usercustomize.py") or base.endswith(".pth"):
        return "changes what gets imported"
    if base.endswith((".so", ".pyd")):
        return "is taken for a compiled module, ahead of the source file of the same name"
    if len(parts) > 2 or (len(parts) == 2 and (SRC / parts[0]).is_dir()):
        # a module added to a package or folder that exists (sqlglot/x.py, tests/dialects/x.py):
        # only an import of its own name finds it
        if base.endswith(".py") and parts[0] in ("sqlglot", "tests") and asked_for_by_name(parts[0], base[:-3]):
            return "has a name that an import at the pinned commit asks for"
        return None
    name = parts[0] if len(parts) == 2 else base.rsplit(".", 1)[0]
    if name in sys.stdlib_module_names or name in sys.builtin_module_names or name in ("sqlglot", "tests", "sqlglotc"):
        return "hides a module of the same name"
    try:
        import importlib.util

        if not _SITE_LOADED:
            import site

            site.main()  # installed packages count too (the selector itself starts without them)
            _SITE_LOADED.append(True)
        return "hides a module of the same name" if importlib.util.find_spec(name) is not None else None
    except Exception:
        return "may hide a module of the same name"
CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


def declared_coding(text):
    """The source encoding a Python file declares in its first two lines, if any."""
    for line in text.split("\n", 2)[:2]:
        m = CODING_RE.match(line)
        if m:
            return m.group(1).lower()
    return None


def same_code(a, b):
    """Whether two syntax trees compile to the same code up to line numbers and docstrings:
    equal once statements that are a bare constant (docstrings among them) are left out."""
    stack = [(a, b)]
    while stack:
        x, y = stack.pop()
        if type(x) is not type(y):
            return False
        if isinstance(x, ast.AST):
            for field in x._fields:
                stack.append((getattr(x, field, None), getattr(y, field, None)))
        elif isinstance(x, list):
            if (x and isinstance(x[0], ast.stmt)) or (y and isinstance(y[0], ast.stmt)):
                x = [n for n in x if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
                y = [n for n in y if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
            if len(x) != len(y):
                return False
            stack.extend(zip(x, y))
        elif isinstance(x, (float, complex)):
            if repr(x) != repr(y):
                return False
        elif x != y:
            return False
    return True


def scope_signature(func):
    """What decides how the names in a function's body resolve and what calling it returns: its
    parameters, the names its own body binds (not those of nested functions and classes), its
    global and nonlocal declarations, and whether it yields or awaits."""
    args = func.args
    params = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
    params += ["*" + a.arg for a in (args.vararg,) if a] + ["**" + a.arg for a in (args.kwarg,) if a]
    binds, declared, flags = set(), set(), set()
    todo = list(func.body)
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            binds.add(node.name)
            todo.extend(node.decorator_list)  # evaluated in this scope, like defaults and bases
            if isinstance(node, ast.ClassDef):
                todo.extend(node.bases)
                todo.extend(k.value for k in node.keywords)
            else:
                todo.extend(node.args.defaults)
                todo.extend(d for d in node.args.kw_defaults if d is not None)
            continue
        if isinstance(node, ast.Lambda):
            todo.extend(node.args.defaults)
            todo.extend(d for d in node.args.kw_defaults if d is not None)
            continue
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            # its own scope: only a `:=` in it binds here
            binds.update(sub.target.id for sub in ast.walk(node) if isinstance(sub, ast.NamedExpr))
            continue
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                binds.add(node.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update((type(node).__name__, name) for name in node.names)
        elif isinstance(node, (ast.Yield, ast.YieldFrom)):
            flags.add("yield")
        elif isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
            flags.add("await")
        elif isinstance(node, ast.alias):
            binds.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                binds.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            if node.name:
                binds.add(node.name)
        elif isinstance(node, ast.MatchMapping):
            if node.rest:
                binds.add(node.rest)
        todo.extend(ast.iter_child_nodes(node))
    return (tuple(params), frozenset(binds), frozenset(declared), frozenset(flags), type(func).__name__)


def bound_names(tree):
    """The names a module's top level and its class bodies bind, as (scope, name): defs, classes,
    assignment targets and imports, also inside if/try/for/with blocks there."""
    out = set()

    def targets(node, scope):
        if isinstance(node, ast.Name):
            out.add((scope, node.id))
        elif isinstance(node, (ast.Tuple, ast.List)):
            for e in node.elts:
                targets(e, scope)
        elif isinstance(node, ast.Starred):
            targets(node.value, scope)

    def walk(body, scope):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.add((scope, st.name))
            elif isinstance(st, ast.ClassDef):
                out.add((scope, st.name))
                walk(st.body, scope + st.name + ".")
            elif isinstance(st, ast.Assign):
                for t in st.targets:
                    targets(t, scope)
            elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
                targets(st.target, scope)
            elif isinstance(st, (ast.Import, ast.ImportFrom)):
                for a in st.names:
                    out.add((scope, a.asname or a.name.split(".")[0]))
            else:
                if isinstance(st, (ast.For, ast.AsyncFor)):
                    targets(st.target, scope)
                if isinstance(st, (ast.With, ast.AsyncWith)):
                    for item in st.items:
                        if item.optional_vars is not None:
                            targets(item.optional_vars, scope)
                for field in ("body", "orelse", "finalbody"):
                    part = getattr(st, field, None)
                    if isinstance(part, list):
                        walk(part, scope)
                for handler in getattr(st, "handlers", None) or ():
                    walk(handler.body, scope)
                for case in getattr(st, "cases", None) or ():
                    walk(case.body, scope)

    walk(tree.body, "")
    return out


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

    def keep_for(self, rel, old, new, everyone, import_checked=False):
        """Tests that may see a different result when comparison `old` becomes `new`. A
        comparison that was also evaluated while modules were imported is only judged this
        way when the caller has the import-time state checked (import_checked): what its
        import-time results built is in that state."""
        if len(old.ops) != 1 or len(new.ops) != 1:
            raise Unknown
        site = self.site(rel, old.lineno, old.col_offset)
        if site is None:
            return everyone  # never evaluated: keep whoever ran the line
        if site["import"] and not import_checked:
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
        self.subprocess = self.subprocess_all = doc.get("subprocess_tests", 0)
        # At the pinned commit that is one test, test_lazy_load, and all its interpreter does
        # is `import sqlglot`: only the files that import loads (CORE_FILES) can change it.
        self.subprocess_imports_core_only = self.subprocess_all == self.test_bit.get(LAZY_LOAD_TEST, -1)
        # every changed span of the current commit: (rel, lo, hi, "function" | "lambda" | "other");
        # "lambda": the body of a lambda in a class- or module-level statement (function code
        # whose lines also run when the statement around it is evaluated, at import)
        self.touched = []
        self._class_users_by_file = None
        self.new_idx = None
        self.last_differs = None
        self.no_import_widen = False  # set to see the selection an unchanged import-time state allows
        self.scope_funcs = {}  # functions of the file in hand whose names resolve differently (scope_changes)
        self.flag_narrowing, self.flag_narrowed, self.code_blocks = True, False, 0  # see select()
        self._test_texts = None  # the test sources, read when a patch changes a test module
        self.defaults_only = False
        self.import_widened = False

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
            func = st.get("func")
            if func is not None and id(func) in self.scope_funcs:
                # its names resolve differently: any line of it may now behave differently
                lo, hi, blk = func.lineno, func.end_lineno, None
            cm = None
            self.last_differs = None
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
            return m
        self.touched.append((rel, lo, hi, "other"))
        if blk is not None and st["kind"] == "stmt":
            in_lambda = self.nested_lambda_mask(rel, st["node"], line, blk)
            if in_lambda is not None:
                return in_lambda
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
                m |= self.unless_state_same(rel, self.class_fallback(rel, st["cls"], idx), node, blk)
            elif in_decorators:
                m |= self.unless_state_same(rel, self.module_fallback(rel, idx), node, blk)
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
        return self.unless_state_same(rel, self.module_fallback(rel, idx, st["node"]), st["node"], blk)

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

    NO_STATE_FILES = {"sqlglot/__main__.py"}  # not imported by the import-time state check

    DEFERRED = (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.GeneratorExp)

    def edit_in_deferred_code(self, node, blk):
        """Whether an edited span of this statement lies inside code that runs later (a lambda
        or def body, a generator expression): the state check leaves code out, so for those
        only coverage can tell. Decorators and bases of a def or class are checked, not its
        body (its own statements are judged on their own)."""
        if blk is None:
            return True
        parts = [node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts = list(node.decorator_list) + list(getattr(node, "bases", [])) + \
                [k.value for k in getattr(node, "keywords", [])]
            if not isinstance(node, ast.ClassDef):
                parts += list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
        spans = []
        for ln, old_text, new_text in zip(blk["removed"], blk["removed_text"], blk["added_text"]):
            lo, hi = changed_range(old_text, new_text)
            spans.append(((ln, lo), (ln, hi)))
        if len(blk["removed"]) != len(blk["added"]):
            return True
        hit = []
        for part in parts:
            for sub in ast.walk(part):
                if isinstance(sub, self.DEFERRED) and sub is not node:
                    start, end = (sub.lineno, sub.col_offset), (sub.end_lineno, sub.end_col_offset)
                    if any(a < end and start < b for a, b in spans):
                        hit.append(sub)
        return hit or False

    def unless_state_same(self, rel, mask, node=None, blk=None):
        """A class- or module-level statement only shapes what importing builds. If the import-
        time state check finds every module and class exactly as before, it changed nothing a
        test can see: the selection without it is empty. This marks the commit for that check
        and returns the usual selection, or nothing on the pass that assumes the check passed.
        Not for an edit inside a lambda or def body there: that is code, which the check
        leaves out."""
        if rel in self.NO_STATE_FILES or not rel.startswith("sqlglot/") or node is None:
            return mask
        deferred = self.edit_in_deferred_code(node, blk)
        if deferred is True:
            return mask
        if not deferred and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # a plain value edit almost always changes what the import builds (that's what it's
            # for), so the check would rarely pay for itself; decorators and bases often don't
            return mask
        self.import_widened = True
        if not self.no_import_widen:
            return mask
        if deferred:
            # the edit is inside a lambda (or generator) this statement builds: with what the
            # import built unchanged, it matters only where that code runs, in tests
            m = 0
            for sub in deferred:
                m |= self.line_union(rel, sub.lineno, sub.end_lineno)
            return m | (self.subprocess & mask)
        if self.touched and self.touched[-1][0] == rel and self.touched[-1][3] == "other":
            self.touched.pop()  # nothing changed: no span for the TPC-DS plan either
        return 0

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
        if narrowed is None:
            return self.unless_state_same(rel, m | self.subprocess, st["node"], blk)
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
                    if any(sub.lineno in IMPORT_RUN_LAMBDAS.get(rel, ()) for sub in ast.walk(v)
                           if isinstance(sub, ast.Lambda)):
                        return None  # an import ran it: what that import built may change too
                    return self.line_union(rel, v.lineno, v.end_lineno)
                if isinstance(k, ast.Attribute) and isinstance(k.value, ast.Name) and k.value.id == "exp":
                    return self.doc["expr_family_users"].get(k.attr, 0)
                return None
        return None

    def lambda_body(self, node, line, blk):
        """The lambda whose body holds a one-line edit to the class- or module-level statement
        `node`, if that body is all the edit changes: the old and the new statement are the
        same tree once that one lambda's body is taken out of both. The innermost such
        lambda; None for any other edit."""
        if len(blk["removed"]) != 1 or len(blk["added"]) != 1:
            return None
        lo, hi = changed_range(blk["removed_text"][0], blk["added_text"][0])
        here, there = (line, lo), (line, hi)
        lam = None
        for sub in ast.walk(node):
            if isinstance(sub, ast.Lambda):
                body = sub.body
                if (body.lineno, body.col_offset) <= here and there <= (body.end_lineno, body.end_col_offset) \
                        and (lam is None or (sub.lineno, sub.col_offset) > (lam.lineno, lam.col_offset)):
                    lam = sub
        if lam is None:
            return None
        new_st, _ = self.new_idx.stmt_at(blk["added"][0])
        if new_st is None or type(new_st["node"]) is not type(node):
            return None
        twin = None
        for sub in ast.walk(new_st["node"]):
            if isinstance(sub, ast.Lambda) and (sub.lineno, sub.col_offset) == (lam.lineno, lam.col_offset):
                twin = sub
                break
        if twin is None:
            return None

        def without_body(stmt, target):
            body, target.body = target.body, ast.Constant(value=None)
            try:
                return ast.dump(stmt)
            finally:
                target.body = body

        if without_body(node, lam) != without_body(new_st["node"], twin):
            return None
        return lam

    def nested_lambda_mask(self, rel, node, line, blk):
        """A one-line edit confined to the body of a lambda that sits somewhere in a class- or
        module-level statement other than directly as a dict value (dict_entry_mask has
        those): in a dict nested in a dict value (the type annotators), a call's argument, a
        comprehension. Evaluating the statement builds the same objects as before except
        for the code that lambda's body runs, so only a test that runs the body can tell, and
        running it fires the events of its lines: the tests that ran those lines, with the
        lambda's lines as the changed function code. None for any other edit, and for a
        lambda whose body ran while modules were imported (IMPORT_RUN_LAMBDAS)."""
        lam = self.lambda_body(node, line, blk)
        if lam is None:
            return None
        lo, hi = changed_range(blk["removed_text"][0], blk["added_text"][0])
        here, there = (line, lo), (line, hi)
        for d in ast.walk(node):  # the same walk as dict_entry_mask: is it one of its lambdas?
            if not isinstance(d, ast.Dict):
                continue
            for k, v in zip(d.keys, d.values):
                if (v.lineno, v.col_offset) <= here and there <= (v.end_lineno, v.end_col_offset):
                    if k is not None and isinstance(v, ast.Lambda):
                        return None
                    break
            else:
                continue
            break
        ran_at_import = IMPORT_RUN_LAMBDAS.get(rel, ())
        if any(sub.lineno in ran_at_import for sub in ast.walk(node) if isinstance(sub, ast.Lambda)
               and (sub.lineno, sub.col_offset) <= (lam.lineno, lam.col_offset)
               and (lam.end_lineno, lam.end_col_offset) <= (sub.end_lineno, sub.end_col_offset)):
            return None  # it, or a lambda around it, ran during an import
        if self.touched and self.touched[-1][0] == rel and self.touched[-1][3] == "other":
            self.touched[-1] = (rel, lam.lineno, lam.end_lineno, "lambda")
        log(f"edit inside a lambda's body in {rel}:{line} narrowed to the tests that ran the lambda")
        return self.line_union(rel, lam.lineno, lam.end_lineno)

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
        # (a changed comparison that ran while modules were imported makes keep_for give up,
        # except on the pass that assumes the import-time state check passed: the statement's
        # lines ran at import, so that pass is only used once the check has confirmed it)
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
                keep |= ops.keep_for(rel, old, new, self.ALL, import_checked=self.no_import_widen)
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
        if not read_at_import and self.touched and self.touched[-1][0] == rel and self.touched[-1][3] == "other":
            # no import-time code reads the name, and the statement itself only binds a literal:
            # importing does what it did (needs_import_check leaves such an edit alone)
            self.touched[-1] = self.touched[-1][:3] + ("constant",)
        old, cur = node.value, new.value
        if self.flag_narrowing and isinstance(old, ast.Constant) and isinstance(cur, ast.Constant) \
                and isinstance(old.value, bool) and isinstance(cur.value, bool) and old.value is not cur.value:
            return self.flag_sites_mask(entry, name, old.value)
        return entry["mask"]

    def flag_sites_mask(self, entry, name, old):
        """A boolean class constant flipped: of the tests that read it, the ones where its value
        can have mattered. The reads are known per line (tools/attrs_plugin.py). At a line
        where the constant only gates the rest of a condition (`if self.FLAG and <rest>:`), and
        its old value let the condition go on to the rest, the flipped constant stops the
        condition there with False: that is the same outcome unless the rest came out true,
        in which case the test ran the lines that follow (_ATTR_EVIDENCE). A test whose first
        difference would be at such a read ran those lines; at any other read, every test that
        read there stays. Only used when the flip is the commit's one code change (select)."""
        sites = entry.get("sites")
        if not sites or not _ATTR_EVIDENCE:
            return entry["mask"]
        m = 0
        for (file, line), readers in sites.items():
            found = _ATTR_EVIDENCE.get((file, line, name))
            if found is not None and found[0] is old:
                m |= readers & self.line_union(file, found[1], found[2])
                self.flag_narrowed = True
            else:
                m |= readers
        if self.flag_narrowed:
            log(f"flag {name} narrowed to the reads where the rest of the condition held")
        return m

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
        self.line_blocks[rel] = [(b["anchor"], len(b["removed"]), len(b["added"])) for b in blocks]
        try:
            new_idx = SourceIndex(new_text)
        except SyntaxError:
            raise Fallback(f"syntax error in {rel}")
        self.new_idx = new_idx
        if declared_coding(old_text) != declared_coding(new_text):
            raise Fallback(f"source encoding declaration changed in {rel}")
        if self.maybe_layout_only(blocks, idx, new_idx) and same_code(idx.tree, new_idx.tree):
            # layout only (line ends, spacing, wrapping, comments) or the text of a docstring:
            # the file compiles to the same code, as with a comment-only hunk, and nothing in
            # sqlglot or its tests reads a docstring
            log(f"{rel}: same code as before")
            return 0
        lost = bound_names(idx.tree) - bound_names(new_idx.tree)
        self.scope_funcs = self.scope_changes(rel, idx, new_idx, blocks, new_text)
        readers = self.doc["file_opens"].get(rel, 0)
        if readers and self.import_statements(idx.tree) != self.import_statements(new_idx.tree):
            # A test that reads this source file as text sees the unpatched file in a check (only
            # imports are overlaid). The one such test, test_typing's redundancy check, reads
            # which module each typing file imports its metadata from: with an import statement
            # changed it can't be checked, so it stays selected.
            self.data_mask |= readers
        m = 0
        for blk in blocks:
            if self.comment_only(blk, idx, new_idx):
                continue
            self.code_blocks += 1
            for ln in blk["removed"]:
                m |= self.mask_for_old_line(rel, idx, ln, blk)
            if blk["added"]:
                m |= self.mask_for_added(rel, idx, new_idx, blk)
        self.scope_funcs = {}
        if lost:
            # a module- or class-level name is gone (a def renamed or removed): other modules'
            # imports of it fail, lookups of it raise, an override stops overriding. Coverage of
            # the old lines doesn't say who relies on the name, so everything is selected (the
            # checks that run the patched sources still apply).
            log(f"{rel} no longer binds {', '.join(sorted(scope + name for scope, name in lost)[:3])}: every test")
            return self.ALL
        return m

    @staticmethod
    def squeezed(lines):
        """The lines' characters without white space and without what follows a `#`."""
        return "".join("".join(t.split("#", 1)[0].split()) for t in lines)

    def maybe_layout_only(self, blocks, idx, new_idx):
        """A cheap necessary condition for an edit that changes no code: every block is comments
        and blank lines, or the same characters once white space and comments are dropped, or
        lies inside string literals on both sides (a docstring's text). same_code then decides."""
        for blk in blocks:
            if all(is_comment_or_blank(t) for t in blk["removed_text"] + blk["added_text"]):
                continue
            if self.squeezed(blk["removed_text"]) == self.squeezed(blk["added_text"]):
                continue
            if all(idx.in_string(ln) for ln in blk["removed"]) and all(new_idx.in_string(ln) for ln in blk["added"]):
                continue
            return False
        return True

    def scope_changes(self, rel, idx, new_idx, blocks, new_text):
        """{id of an old function node: True} for the functions an edit changes as a whole: the
        names its body binds, its parameters, its global/nonlocal declarations, or whether it is
        a generator (scope_signature). Then lines the edit doesn't touch behave differently (a
        name read before a new assignment to it is now an unbound local; a call returns a
        generator), so every test that entered the function is selected (mask_for_old_line).
        An edit that adds or removes lines also has the new file compiled: ast.parse doesn't
        report what only the compiler rejects (`break` outside a loop, a duplicate argument)."""
        changed = {}
        compiled = False
        for blk in blocks:
            if all(is_comment_or_blank(t) for t in blk["removed_text"] + blk["added_text"]):
                continue
            old_lines = blk["removed"] or [blk["anchor"] - 1, blk["anchor"]]
            new_lines = blk["added"] or [blk["new_anchor"] - 1, blk["new_anchor"]]
            old_funcs, new_funcs = {}, {}
            for lines, index, funcs in ((old_lines, idx, old_funcs), (new_lines, new_idx, new_funcs)):
                for ln in lines:
                    st, _ = index.stmt_at(ln)
                    func = st.get("func") if st is not None and st["scope"] == "function" else None
                    if func is not None:
                        funcs[id(func)] = func
            differs = False
            for func in old_funcs.values():
                twins = [g for g in new_funcs.values() if g.name == func.name]
                if not twins or any(scope_signature(g) != scope_signature(func) for g in twins):
                    changed[id(func)] = True
                    differs = True
                    log(f"{rel}: the names of {func.name} resolve differently: every test that entered it")
            if (differs or len(blk["removed"]) != len(blk["added"])) and not compiled:
                compiled = True
                try:
                    compile(new_text.encode("utf-8", "surrogateescape"), rel, "exec", dont_inherit=True)
                except (SyntaxError, ValueError) as error:
                    raise Fallback(f"{rel} does not compile ({type(error).__name__})")
        return changed

    @staticmethod
    def import_statements(tree):
        return [ast.dump(n) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]

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
        if any(idx.in_string(ln) for ln in blk["removed"]):
            return False
        if any(new_idx.in_string(ln) for ln in blk["added"]):
            return False
        anchor = blk["anchor"]
        if (anchor - 1) in idx.continued:
            return False
        return True

    def test_texts(self):
        if self._test_texts is None:
            self._test_texts = {}
            for path in (SRC / "tests").rglob("*.py"):
                try:
                    self._test_texts[path.relative_to(SRC).as_posix()] = path.read_text("utf-8", "surrogateescape")
                except OSError:
                    continue
        return self._test_texts

    def files_naming(self, rel, word):
        """The other Python files under tests/ that mention this name as a whole word: the users
        of a class (33 dialect test files take Validator from test_dialect)."""
        pattern = re.compile(r"\b" + re.escape(word) + r"\b")
        return {other for other, text in self.test_texts().items() if other != rel and pattern.search(text)}

    def importers(self, rels):
        """The Python files under tests/ that import one of these test modules, directly or
        through one another (an import statement naming the module's stem), with the modules
        themselves."""
        texts = self.test_texts()
        lines = {other: [ln for ln in text.split("\n") if ln.lstrip().startswith(("import ", "from "))]
                 for other, text in texts.items()}
        found, todo = set(rels), list(rels)
        while todo:
            stem = os.path.basename(todo.pop())[:-3]
            if stem == "__init__":
                return set(texts)
            pattern = re.compile(r"\b" + re.escape(stem) + r"\b")
            for other, imports in lines.items():
                if other not in found and any(pattern.search(ln) for ln in imports):
                    found.add(other)
                    todo.append(other)
        return found

    def files_mask(self, rels):
        """Every test of these test files; all tests if one of them holds shared code rather
        than tests of its own (anything may run it)."""
        m = 0
        for other in rels:
            if not os.path.basename(other).startswith("test_"):
                return self.ALL
            m |= self.doc["test_files"].get(other, 0)
        return m

    def test_source(self, rel, fd):
        doc = self.doc
        stem = os.path.basename(rel)[:-3]
        if not stem.startswith("test_"):
            # tests/helpers.py and the like hold no tests of their own: other test modules
            # import them, so any test may run this code. A script that no test module imports
            # (tests/leakcheck.py, tests/gen_fixtures.py; pytest collects test_*.py only) runs
            # in no test.
            if stem == "__init__" or stem.endswith("_test") or self.importers({rel}) - {rel}:
                raise Fallback(f"shared test code {rel}")
            return 0
        own = doc["test_files"].get(rel, 0)
        # what a change to the module as a whole reaches: its own tests, and those of the test
        # modules that import it, directly or not (their import runs, or fails with, the edited code)
        file_mask = own | self.files_mask(self.importers({rel}) - {rel})
        try:
            old_text, idx = self.index(rel)
        except (OSError, SyntaxError):
            self.verifiable = False
            return file_mask
        new_text, blocks = apply_hunks(old_text, fd["hunks"])
        self.patched[rel] = new_text
        self.line_blocks[rel] = [(b["anchor"], len(b["removed"]), len(b["added"])) for b in blocks]
        try:
            new_idx = SourceIndex(new_text)
        except SyntaxError:
            return file_mask
        if declared_coding(old_text) != declared_coding(new_text):
            raise Fallback(f"source encoding declaration changed in {rel}")
        if ast.dump(idx.tree) == ast.dump(new_idx.tree):
            return 0  # layout only: the module compiles to the same code
        m = 0
        for blk in blocks:
            if self.comment_only(blk, idx, new_idx):
                continue
            self.code_blocks += 1
            # the statements the block replaces (or is put between) in the old file, then the
            # ones its added lines belong to in the new file: a method put right after another
            # (a new setUp, say) is part of neither neighbour
            sides = [(idx, ln) for ln in (blk["removed"] or [blk["anchor"] - 1, blk["anchor"]])]
            sides += [(new_idx, ln) for ln in blk["added"]]
            for index, ln in sides:
                st, _ = index.stmt_at(ln)
                if st is None and index is new_idx:
                    continue  # an added blank or comment line outside every statement
                if st is None or st["scope"] == "module" and st["kind"] != "class":
                    return file_mask
                cls = st["cls"]
                users = self.files_naming(rel, cls.split(".")[0]) if cls else None
                if users:
                    # a class other test modules build on: its code runs in their tests, and in
                    # those of the modules importing them
                    return file_mask | self.files_mask(self.importers(users))
                func = st["func"] if st["scope"] == "function" else (st["node"] if st["kind"] == "def" else None)
                if cls and func is not None:
                    test_id = f"{rel}::{cls}::{func.name}"
                    tm = self.test_bit.get(test_id)
                    m |= tm if tm else doc["test_classes"].get(f"{rel}::{cls}", own)
                elif cls:
                    m |= doc["test_classes"].get(f"{rel}::{cls}", own)
                else:
                    return file_mask
        return m

    def unchanged_code(self, rel, fd):
        """Whether a patch to a Python file leaves its code as it was (comments, layout, docstrings)."""
        try:
            old_text, idx = self.index(rel)
            new_text, blocks = apply_hunks(old_text, fd["hunks"])
            new_idx = SourceIndex(new_text)
        except (OSError, SyntaxError, Fallback, ValueError):
            return False
        if declared_coding(old_text) != declared_coding(new_text):
            return False
        return self.maybe_layout_only(blocks, idx, new_idx) and same_code(idx.tree, new_idx.tree)

    def tpc_file_mask(self, rel, fd, readers):
        """Which readers of a TPC query file an edit to it can reach. TestExecutor.setUpClass loads
        both files for the whole class, so all 60 of its tests count as readers, but only three of
        them use what was loaded (TPC_QUERY_FILES, from the tests' source): while the edited file
        still loads into the same number of cases, the others see no difference. A test that
        reads whole cases is selected if any case changed; one that reads only the meta lines
        and the input query, if one of those changed. Anything unexpected keeps every reader."""
        spec = TPC_QUERY_FILES[rel]
        users = 0
        for name in spec["cases"] + spec["inputs"]:
            bit = self.test_bit.get(name)
            if not bit or not bit & readers:
                return readers
            users |= bit
        if readers & ~(users | self.doc["test_classes"].get("tests/test_executor.py::TestExecutor", 0)):
            return readers  # a reader this rule doesn't know about
        try:
            old_text = (SRC / rel).read_text("utf-8")
            new_text, _ = apply_hunks(old_text, fd["hunks"])
            old, new = fixture_pairs(old_text), fixture_pairs(new_text)
        except Exception:  # the patch doesn't apply here, or the new file no longer loads
            return readers
        if len(old) != len(new):
            return readers
        changed = [i for i in range(len(old)) if old[i] != new[i]]
        inputs = [i for i in changed if old[i][:2] != new[i][:2]]
        m = 0
        for name in (spec["cases"] if changed else ()) + (spec["inputs"] if inputs else ()):
            m |= self.test_bit[name]
        log(f"{rel}: {len(changed)} cases changed ({len(inputs)} in their input or meta lines): "
            f"{bin(m).count('1')} of {bin(readers).count('1')} readers")
        return m

    def select(self, text):
        """The tests one patch can break (a bit mask), with what the checks need left on self."""
        self.flag_narrowing = True
        m = self.select_once(text)
        if self.flag_narrowed and self.code_blocks != 1:
            # what a flag was narrowed with describes the unpatched code around its reads, so
            # it only holds when the flip is the commit's one change to Python code
            self.flag_narrowing = False
            try:
                m = self.select_once(text)
            finally:
                self.flag_narrowing = True
        return m

    def select_once(self, text):
        doc = self.doc
        m = 0
        self.flag_narrowed = False
        self.code_blocks = 0  # changed blocks of Python code (comment-only ones aside)
        self.patched = {}
        self.line_blocks = {}  # rel -> [(anchor, removed, added)]: the change blocks, for line numbers
        self.verifiable = True
        self.data_files = set()  # changed files we don't overlay: their readers can't be checked
        self.state_masks = []  # bindings the import-time state check leaves out (the edited ones)
        self.import_widened = False
        self.data_mask = 0
        self.touched = []
        diffs = parse_patch(text)
        if not diffs:
            raise Fallback("no file changes found in the patch")
        self.subprocess = self.subprocess_all
        if self.subprocess_imports_core_only and not any(self.reaches_core_import(fd) for fd in diffs):
            self.subprocess = 0
        for fd in diffs:
            rel = fd["new"] or fd["old"]
            old_rel = fd["old"]
            if rel is None:
                if fd["hunks"] or fd["binary"] or fd["new_file"] or fd["deleted"]:
                    raise Fallback("a change to a file whose name can't be read")
                continue
            if any(p and "/" in p and p.split("/", 1)[0].endswith((".egg-info", ".dist-info")) for p in (rel, old_rel)):
                # package metadata beside the sources: the repository root is on sys.path, so
                # entry points (the dialect plugins) and versions are read from it
                raise Fallback(f"package metadata {rel}")
            if fd["mode"] and not fd["hunks"] and not fd["binary"] and not fd["deleted"] and not fd["new_file"] \
                    and (not old_rel or old_rel == rel):
                continue  # only the file's mode changed: what Python and the tests read is as before
            if fd["binary"] or fd["deleted"] or (old_rel and old_rel != rel):
                paths = {p for p in (rel, old_rel) if p}
                if any(p.endswith(IMPORTABLE) or p in self.config or p in PYTEST_CONFIGS
                       or (os.path.basename(p) in PYTEST_CONFIGS and p.startswith("tests/"))
                       or os.path.basename(p) in ("conftest.py", "__init__.py") for p in paths):
                    raise Fallback(f"binary, deleted or renamed file {rel}")
                # a file that isn't code or configuration and is replaced, removed or moved reaches
                # the tests that opened it under its old or new name (the tests open their data
                # files by name; the one test that lists a directory reads Python sources); no
                # check runs them
                for p in paths:
                    readers = doc["file_opens"].get(p, 0)
                    m |= readers
                    self.data_files.add(p)
                    self.data_mask |= readers
                self.touched.append((rel, 0, 0, "other"))
                continue
            readers = doc["file_opens"].get(rel, 0)  # tests that read this file's text
            if rel in TPC_QUERY_FILES and readers and fd["hunks"] and not fd["new_file"]:
                readers = self.tpc_file_mask(rel, fd, readers)
            m |= readers
            if rel in ("pyproject.toml", "setup.cfg") and fd["hunks"] and not fd["new_file"] and all(
                    piece[:1] == " " or is_comment_or_blank(piece[1:])
                    or (rel == "setup.cfg" and piece[1:].lstrip().startswith(";"))
                    for _, _, body in fd["hunks"] for piece in body):
                # only comment and blank lines come or go (pytest's parser of setup.cfg drops a
                # line that starts with # or ; wherever it stands): neither file has a multi-line
                # string at the pinned commit that such a line could land in, so each parses to
                # the same settings
                continue
            if rel == "pyproject.toml" and fd["hunks"] and not fd["new_file"] and pyproject_inert(fd["hunks"]):
                continue  # still TOML, still without settings for pytest
            if rel == "setup.cfg" and fd["hunks"] and not fd["new_file"] and setup_cfg_inert(fd["hunks"]):
                continue  # still parses, still without a section for pytest
            if rel == "setup.py" and fd["hunks"] and not fd["new_file"]:
                # the root setup.py: pytest looks whether it exists, and nothing runs or reads it
                # when the tests run from the checkout (tests/test_sqlglotc.py runs sqlglotc's)
                continue
            if os.path.basename(rel) in PYTEST_CONFIGS and rel.startswith("tests/"):
                # pytest takes its settings and root from the first such file it finds going up
                # from the paths it is given
                raise Fallback(f"pytest config file {rel}")
            if rel.endswith(".py") and rel.startswith("tests/") and fd["hunks"] and not fd["new_file"] \
                    and self.unchanged_code(rel, fd):
                continue  # shared test code (tests/helpers.py) with only its layout or comments changed
            if rel in self.config or os.path.basename(rel) == "conftest.py" or rel in PYTEST_CONFIGS:
                raise Fallback(f"config file {rel}")
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
                self.code_blocks += rel.endswith(".py")
                if rel.endswith(IMPORTABLE) or os.path.basename(rel) in ("__init__.py", "sitecustomize.py"):
                    reason = new_module_risk(rel)
                    if reason:
                        raise Fallback(f"new file {rel} {reason}")
                    # a test that reads the sources of a package lists its directory (test_typing
                    # reads every module of sqlglot/typing): a new module there is read too
                    folder = os.path.dirname(rel)
                    for other, readers in doc["file_opens"].items():
                        if other.endswith(".py") and os.path.dirname(other) == folder:
                            m |= readers
                continue  # any other new file: nothing imports or opens it, so it can't change a test
            if rel.endswith(".py"):
                if not fd["hunks"]:
                    raise Fallback(f"no hunks for {rel}")
                if rel.startswith("sqlglot/"):
                    m |= self.python_source(rel, fd)
                    continue
                self.code_blocks += 1  # Python outside sqlglot/ (test_source counts its blocks again)
                if rel.startswith("tests/"):
                    m |= self.test_source(rel, fd)
                elif not rel.startswith(UNBUILT_DIRS):
                    # Python that the build of the compiled extension may read (setup scripts, sqlglotc/)
                    m |= doc["test_files"].get("tests/test_sqlglotc.py", 0) | doc["test_files"].get("tests/test_build.py", 0)
            self.touched.append((rel, 0, 0, "other"))
        return m

    @staticmethod
    def reaches_core_import(fd):
        """Whether this file's change could alter what a bare `import sqlglot` does: a file that
        import loads, or anything that isn't plainly another sqlglot module or under tests/."""
        rel = fd["new"] or fd["old"]
        if rel is None or fd["binary"] or fd["deleted"] or fd["new_file"] or (fd["old"] and fd["old"] != rel):
            return True
        if rel.startswith("tests/"):
            return False  # the interpreter it starts imports nothing from tests/
        return rel in CORE_FILES or not (rel.startswith("sqlglot/") and rel.endswith(".py"))

    def code_touched(self):
        """The changed spans outside data files (those reach only the tests that open them)."""
        return [t for t in self.touched if t[0] not in self.data_files]

    def plain_function_commit(self):
        """Whether every code change is ordinary function code in sqlglot/: nothing an import
        builds changed, so a piece of a test that never runs a changed line does what it did
        before. A commit may also change data files (a fixture next to the code edit): that
        reaches only the tests that open them, which the callers leave out."""
        touched = self.code_touched()
        return bool(self.verifiable and self.patched and touched
                    and all(rel.startswith("sqlglot/") for rel in self.patched)
                    and all(kind in ("function", "lambda") and rel.startswith("sqlglot/")
                            for rel, _, _, kind in touched))

    def bare_ok(self):
        """Whether a check of this commit may skip the setUpClass of BARE_CLASSES: every change
        is ordinary function code in sqlglot/ (so nothing an import builds changed), and none
        of it is in a file that setUpClass runs code from."""
        return self.plain_function_commit() and all(rel not in BARE_SETUP_FILES for rel, _, _, _ in self.code_touched())

    def fixture_entries(self, tid):
        """For a fixture-driven test (one that loops over the entries of a .sql fixture), the
        entries whose processing can run a changed line: ({"calls": the loader calls as
        recorded, "keep": {call: entry indexes}}, their share of all entries, the CPU ms of a
        full run). None when the whole test has to run: not such a test, a changed line every
        entry depends on, or one no entry accounts for. Only for a plain_function_commit."""
        data = load_fixtures()
        entry = data.get(tid) if data is not None else None
        if entry is None:
            return None
        hit = 0
        for rel, lo, hi, _ in self.code_touched():
            base, per = entry["base"].get(rel), entry["lines"].get(rel)
            for line in range(lo, hi + 1):
                if base and line in base:
                    return None
                if per:
                    hit |= per.get(line, 0)
        if not hit:
            return None
        keep, start = {}, 0
        for k, (_, _, count) in enumerate(entry["calls"]):
            part = (hit >> start) & ((1 << count) - 1)
            start += count
            if part:
                keep[k] = [i for i in range(count) if part >> i & 1]
        return {"calls": entry["calls"], "keep": keep}, hit.bit_count() / max(1, entry["entries"]), entry["full_ms"]

    # ---- the 21 s TPC-DS executor test: replay what the edit can reach instead of running it

    def tpcds_plan(self, chosen, duration, tpcds_number, unreadable=()):
        """A replay plan when it is provably enough and clearly cheaper than the test, else None.

        Only for commits whose every code change is ordinary function code in sqlglot/ (no
        class or module level, nothing run at import, no test files), none of which runs in
        the test's table setup; a changed data file is fine if the test never opens it
        (unreadable: the tests that open one). Each query whose recorded coverage reaches a changed line
        is checked by what that line can affect there: if only parsing, optimize() or the
        duckdb transpilation, its optimizer output is compared ("front"); if also the
        debug-log rendering execute() does next, that rendering is checked too ("log"); if
        planning or execution (or the digest code), its rows ("full"). The duckdb
        transpilation, the expected side of the test's comparison, is always compared."""
        if not self.plain_function_commit() or tpcds_number in unreadable:
            return None
        touched = self.code_touched()
        data = load_tpcds()
        if data is None:
            return None

        def hits(sets, rel, lo, hi):
            lines = sets.get(rel)
            return bool(lines) and any(ln in lines for ln in range(lo, hi + 1))

        spans = [(rel, lo, hi) for rel, lo, hi, _ in touched]
        # A lambda's lines are in the setup's coverage whenever its module was imported by then,
        # since evaluating the statement around it runs them; its body didn't run there (the
        # setup is the imports and building the tables, and nested_lambda_mask refuses lambdas
        # whose body ran in either), so only function code is held to this.
        if any(kind != "lambda" and hits(data["setup_sets"], rel, lo, hi) for rel, lo, hi, kind in touched):
            return None
        # Per query, what the changed lines can affect decides how it's checked: "front"
        # (parse, optimize, duckdb transpile: the optimizer output digest and the duckdb SQL),
        # "log" (also the dropped debug-log rendering: it must not raise or alter the
        # expression), or "full" (planning and execution, or the digest code itself: the rows).
        digest_hit = any(hits(data["digest_sets"], *s) for s in spans)
        modes, reached = {}, []
        for q in data["queries"]:
            in_exec = any(hits(q["exec_sets"], *s) for s in spans)
            in_log = any(hits(q["log_sets"], *s) for s in spans)
            if not (in_exec or in_log or any(hits(q["front_sets"], *s) for s in spans)):
                continue
            modes[q["index"]] = "full" if in_exec or digest_hit else ("log" if in_log else "front")
            reached.append(q)
        if not reached:
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
        budget = TPCDS_BUDGET * saving
        mode = "full" if full == len(reached) else "front"
        log(f"TPC-DS replay planned: {len(reached)} queries ({full} full, "
            f"{sum(1 for m in modes.values() if m == 'log')} with the log line), "
            f"est {estimate:.1f}s to save {saving:.1f}s, budget {budget:.1f}s")
        return {"mode": mode, "modes": modes, "queries": [q["index"] for q in reached],
                "estimate": estimate, "budget": budget}

    # ---- test_generator_memory: rerun its per-dialect check only for dialects the edit can reach

    def genmem_plan(self, chosen, duration, genmem_number, unreadable=()):
        """Which dialects to recheck, or None to keep the test as is.

        The test builds every dialect's generator 1,100 times and checks that the last 1,000
        builds leave under 1 KB behind. Only sqlglot modules that are loaded when a dialect's
        generator is built can change that dialect's result (tools/build_genmem.py records
        them per dialect, from a fresh interpreter). Only for commits whose code changes are
        all in sqlglot/ Python files; a changed data file is fine if the test never opens it."""
        if not self.verifiable or not self.patched or genmem_number in unreadable:
            return None
        rels = set(self.patched) | {rel for rel, _, _, _ in self.code_touched()}
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
# measured: importing a test module and its dialects. TestExecutor's setUpClass loads the
# TPC-H and TPC-DS tables (1.1 s) unless the check may skip it (BARE_CLASSES).
FILE_OVERHEAD_MS = {"tests/test_executor.py": 4000}
DEFAULT_FILE_OVERHEAD_MS = 150
BARE_FILE_OVERHEAD_MS = 300
# Fixture-driven tests (they loop over the entries of a .sql fixture) are checked on just the
# entries whose processing can run a changed line, when those are at most this share of them.
# The estimate: a fixed part, plus that share of a full run's CPU with a margin for the pool.
FIXTURE_FILES = {"tests/test_optimizer.py", "tests/test_transpile.py", "tests/test_serde.py"}
FIXTURE_MAX_SHARE = 0.2
FIXTURE_SETUP_MS = 60
FIXTURE_MARGIN = 1.5
# A class whose setUpClass a check may skip, and the only sqlglot files that setUpClass runs
# code from (traced at the pinned commit): when a commit changes nothing but ordinary function
# code in other sqlglot files, setUpClass does exactly what it did before, so it can't fail,
# and a test that never touches what it builds runs the same without it.
BARE_CLASSES = {"tests/test_executor.py": "TestExecutor"}
BARE_SETUP_FILES = {"sqlglot/executor/table.py"}
# The tests of those classes that touch what setUpClass builds, as fixture_tests() finds them
# in the pinned test file (kept here because parsing that file costs 50 ms a run).
BARE_NEEDS_SETUP = {
    ("tests/test_executor.py", "TestExecutor"): frozenset(("test_execute_tpcds", "test_execute_tpch", "test_optimized_tpch")),
}
# test_optimizer's heavy tests. Most run fixtures through a process pool, so a check costs
# more CPU than their recorded wall time, and they are the tests an edit most often breaks:
# a check that finds a failure has only cost CPU. Each is charged 3.7 times its measured check
# CPU, the margin the fixed overheads above give a small check of ordinary tests (ms beyond
# wall; with their true cost the sample lost 1.2 s: one check paid off, two found failures).
# The file's other tests cost what they take, like any other test.
EXTRA_CPU_MS = {
    "tests/test_optimizer.py::TestOptimizer::" + name: ms for name, ms in (
        ("test_tpcds", 13300), ("test_qualify_columns", 5750), ("test_simplify", 3900),
        ("test_merge_subqueries", 3800), ("test_optimize_in_nulls", 3800), ("test_annotate_funcs", 3250),
        ("test_optimize", 2850), ("test_scope", 2400), ("test_optimize_correlated_all", 2000),
        ("test_unnest_subqueries", 1450), ("test_tpch", 1250), ("test_optimize_scalar_subquery_predicates", 1100),
        ("test_canonicalize_internal_names", 1050), ("test_pushdown_predicates", 1050),
        ("test_qualify_semi_anti_join_alias_shadowing_column", 1050),
        ("test_qualify_struct_star_long_cte_chain", 1000), ("test_simplify_string_comparisons", 750))
}


MAX_TEST_MS = 3000  # longer tests are cheaper to schedule than to run here
SKIP_FILES = {
    "tests/test_sqlglotc.py", "tests/test_build.py", "tests/test_docs.py",
    "tests/test_dialect_imports.py", "tests/test_dialect_entry_points.py",
    "tests/test_integration_loader.py",
}
SKIP_TESTS = set()


_FIXTURE_TESTS = {}


def fixture_tests(rel, cls_name):
    """The class's test methods that, directly or through anything they call by name, touch
    an attribute its setUpClass sets (read from the pinned test file); None if that can't be
    told, e.g. when the module has fixtures of its own."""
    key = (rel, cls_name)
    if key in _FIXTURE_TESTS:
        return _FIXTURE_TESTS[key]
    out = None
    try:
        tree = ast.parse((SRC / rel).read_text("utf-8", errors="surrogateescape"))
        functions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name)
        methods = {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        setup = methods.get("setUpClass")
        made, opaque = set(), "setUpModule" in functions or "tearDownModule" in functions
        for node in ast.walk(setup) if setup is not None else ():
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                made.add(node.attr)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("setattr", "vars"):
                opaque = True
        if not opaque:
            uses = {}  # per function or method: the attribute names and the bare names it mentions
            for name, fn in {**functions, **methods}.items():
                attrs, names = set(), set()
                for node in ast.walk(fn):
                    if isinstance(node, ast.Attribute):
                        attrs.add(node.attr)
                    elif isinstance(node, ast.Name):
                        names.add(node.id)
                uses[name] = (attrs, names)

            def needs(name, seen):
                if name in seen:
                    return False
                seen.add(name)
                attrs, names = uses[name]
                if made & attrs or "__dict__" in attrs or {"getattr", "vars"} & names:
                    return True
                return any(needs(other, seen) for other in (attrs & set(methods)) | (names & set(functions)))

            always = [m for m in ("setUp", "tearDown") if m in methods]
            out = {name for name in methods if name.startswith("test")
                   and (needs(name, set()) or any(needs(m, set()) for m in always))}
    except Exception:
        out = None
    _FIXTURE_TESTS[key] = out
    return out


def bare_classes(test_ids):
    """Of BARE_CLASSES, the (file, class) pairs whose tests in this check all run the same
    without setUpClass."""
    out = set()
    for rel, cls_name in BARE_CLASSES.items():
        mine = [t.split("::")[2] for t in test_ids if t.startswith(f"{rel}::{cls_name}::")]
        if not mine:
            continue
        need = BARE_NEEDS_SETUP.get((rel, cls_name))
        if need is None:
            need = fixture_tests(rel, cls_name)
        if need is not None and not need & set(mine):
            out.add((rel, cls_name))
    return out


def pack_cost(numbers, duration):
    workers = pack(numbers, duration)
    if not workers:
        return 0
    return max(sum(duration[n] for n in w) for w in workers) + WORKER_MS * len(workers)


def plan_check(chosen, duration, info, excluded=(), want_gain=False, bare=False, cost_ms=None):
    """The cheapest-first prefix of runnable tests whose check is expected to pay off (with
    want_gain, the ms it is expected to save if they all pass, instead). bare: the check may
    skip the setUpClass of BARE_CLASSES. cost_ms: what running some tests is expected to cost
    instead of their recorded time (a fixture-driven test checked on a few entries)."""
    cost_ms = cost_ms or {}

    def cost(n):
        return cost_ms[n] if n in cost_ms else duration[n] + info[n].get("extra", 0)

    runnable = sorted((n for n in chosen if info[n]["runnable"] and n not in excluded), key=lambda n: (cost(n), n))
    if not runnable:
        return 0 if want_gain else []

    def file_ms(f):
        if bare and f in BARE_CLASSES:
            return BARE_FILE_OVERHEAD_MS
        return FILE_OVERHEAD_MS.get(f, DEFAULT_FILE_OVERHEAD_MS)

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
            run_ms += cost(n) * TIME_FACTOR
            files.add(info[n]["file"])
        prev = k
        overhead = COMMIT_OVERHEAD_MS + sum(file_ms(f) for f in files)
        rest = sorted(chosen_set - set(runnable[:k]))
        gain = base - pack_cost(rest, duration) - run_ms - overhead
        if gain > best_gain:
            best_gain, best_k = gain, k
    return best_gain if want_gain else runnable[:best_k]


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
# {(file, line, name): (value, lo, hi)}: at this read the constant only gates the rest of a
# condition, and while it is `value` its value mattered only in tests that ran lines lo..hi
# (tools/build_attr_owners.py says how that is established)
_ATTR_EVIDENCE = {}
_FIXTURES = None


def load_fixtures():
    """Per fixture-driven test, the lines each fixture entry's processing runs
    (tools/build_fixture_cov.py), one pickle per test; loaded on first use."""
    global _FIXTURES
    if _FIXTURES is None:
        try:
            with gzip.open(FIXTURES_DATA, "rb") as fh:
                _FIXTURES = Blobs(pickle.load(fh)["by_test"])
        except Exception as error:
            log(f"no fixture-entry data ({error!r}); fixture-driven tests are checked whole")
            _FIXTURES = False
    return _FIXTURES or None


def load_attr_owners():
    """Per class constant, the tests whose reads resolved to it (tools/attrs_plugin.py), packed
    per source file; loaded on first use."""
    global _ATTR_OWNERS, _ATTR_EVIDENCE
    if _ATTR_OWNERS is None:
        try:
            with gzip.open(ATTR_OWNERS_DATA, "rb") as fh:
                data = pickle.load(fh)
            _ATTR_OWNERS = Blobs(data["by_file"])
            _ATTR_EVIDENCE = data.get("evidence") or {}
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


def tpcds_rows(data, name):
    """The rows of one TPC-DS table. The rows are most of the data file and only a replay that
    runs a query to the end reads them, for the tables that query uses, so each table stays
    pickled until then (tools/pack_tpcds.py)."""
    tables = data.setdefault("tables", {})
    if name not in tables:
        tables[name] = pickle.loads(data["table_blobs"][name])
    return tables[name]


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
    if _PRELOADED and set(patched) & _PRELOADED:
        # the parent's sqlglot includes a file this patch changes: import all of it again
        for name in [n for n in sys.modules if n == "sqlglot" or n.startswith("sqlglot.")]:
            del sys.modules[name]
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
    # a hard stop: a changed plan can make one query run far longer than recorded
    signal.signal(signal.SIGPROF, lambda *_: os._exit(3))
    if budget != float("inf"):
        signal.setitimer(signal.ITIMER_PROF, budget)
    overlay = enter_overlay(patched)
    data = load_tpcds()
    from sqlglot import find_tables, parse_one, transpile
    import sqlglot.executor as executor_module
    from sqlglot.executor import execute
    from sqlglot.executor.table import Table

    schema = pickle.loads(pickle.dumps(data["schema"]))
    # A run that stops right after optimize() never reads a row: execute() only wraps the
    # tables it is given before it optimizes. Those runs get tables without rows, and the real
    # ones (unpickled, then pickled across as the test's pool does) are built for the first
    # run that goes to the end.
    empty_tables = {t: Table(columns=c, rows=[]) for t, c in data["schema"].items()}
    loaded = {}

    def real_table(name):
        if name not in loaded:
            built = Table(columns=data["schema"][name], rows=tpcds_rows(data, name))
            loaded[name] = pickle.loads(pickle.dumps(built))
        return loaded[name]

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

    started = [0.0]  # recorded CPU of the work begun so far

    def pace(seconds):
        """About to start work recorded at this much CPU: re-arm the stop for what that allows."""
        started[0] += max(seconds, 0.0)
        left = min(budget, TPCDS_SETUP_S + TPCDS_PACE * started[0]) - spent()
        if left <= 0:
            os._exit(3)
        signal.setitimer(signal.ITIMER_PROF, left)

    def cost(q):
        mode = modes[q["index"]]
        return q["cpu"] if mode == "full" else q["front_cpu"] + (q.get("log_cpu", 0.01) if mode == "log" else 0.0)

    def run(q):
        """One query the way the test's pool worker runs it; True if its rows are unchanged."""
        current.clear()
        current["q"] = q
        expression = pickle.loads(pickle.dumps(parse_one(q["sql"])))
        to_end = modes.get(q["index"]) == "full" or q["index"] in optimized or not hooked
        used = {t.name: real_table(t.name) if to_end else empty_tables[t.name] for t in find_tables(expression)}
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
    hooked = any(m != "full" for m in modes.values())
    if hooked:
        executor_module.optimize = optimize_hook
    try:
        for i in plan["queries"]:
            q = by_index[i]
            pace(cost(q))
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
                pace(q["cpu"] - q["front_cpu"])
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
                pace(q["cpu"] - q["front_cpu"])
                if run(q) is not True or spent() > budget:
                    if debug:
                        print(f"tpcds child: query {q['index']} differs or over budget, spent={spent():.2f}s "
                              f"rows={current.get('rows_debug')}", file=sys.stderr)
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


def child_maxnodes(patched, plan, wfd):
    """In a forked child: test_max_nodes on the patched sources, with its two limit asserts on
    prefixes of its statement (see MAXNODES_TEST). Reports the test as failing on any doubt;
    an exception or running past the budget fails the child, which keeps the test."""
    import importlib
    import shutil
    import signal

    signal.signal(signal.SIGPROF, lambda *_: os._exit(3))
    signal.setitimer(signal.ITIMER_PROF, plan["budget"])
    overlay = enter_overlay(patched)
    importlib.import_module("tests.test_parser")  # an import error fails the test (and this child)
    from sqlglot import exp, parse_one
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.errors import ParseError

    phrase = "Maximum number of AST nodes"

    def raises(sql, limit):
        try:
            parse_one(sql, max_nodes=limit)
        except ParseError as error:
            return str(error)
        return None

    expr = "x" + "=x" * 100

    def statement(copies):
        return "SELECT " + ",".join([expr] * copies)

    def count(copies):
        sql = statement(copies)
        dialect = Dialect.get_or_raise(None)
        parser = dialect.parser(max_nodes=10**12)
        parser.parse(dialect.tokenize(sql), sql)
        return parser._node_count

    # the asserts before the big statement, as written
    ok = isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)
    cols = [f"col{i}" for i in range(500)]
    ok = ok and isinstance(parse_one(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000), exp.Select)
    per = 0
    if ok:
        # 10,000 copies against 1,000,000: exact, not with a margin (the pinned sources count
        # 1,001 + 100 per further copy, 1,000,001 in all)
        c10, c40, c160 = count(10), count(40), count(160)
        per, rest = divmod(c160 - c40, 120)
        ok = rest == 0 and per >= 1 and c40 - c10 == 30 * per and c10 + 9990 * per > 1_000_000
    if ok:
        # the limit trips on the statement's last node there: the same on a prefix, through
        # parse_one, with a limit one below its count (and not with a limit equal to it)
        msg = raises(statement(40), c40 - 1)
        ok = msg is not None and phrase in msg and f"({c40 - 1})" in msg
        ok = ok and isinstance(parse_one(statement(40), max_nodes=c40), exp.Select)
    if ok:
        # 50,000 trips mid-statement: a prefix just long enough to get there
        msg = raises(statement(60_000 // per + 20), 50_000)
        ok = msg is not None and "(50000)" in msg and phrase in msg
    if ok:
        # the asserts after it, as written
        ok = raises("SELECT " + ",".join(f"x={i}" for i in range(100)), 5) is not None
        ok = ok and isinstance(parse_one("SELECT 1, 2, 3"), exp.Select)
    shutil.rmtree(overlay, ignore_errors=True)
    os.write(wfd, json.dumps([] if ok else [MAXNODES_TEST]).encode())
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
    # fixture-driven tests run on a subset of their entries: {test id: {"calls", "keep"}}
    entries = limits.get("__entries__") or {}
    current = {"tid": None, "calls": 0}

    def filter_loaders(mod):
        """The module's fixture loaders, yielding only the planned entries to a test that has
        a plan (and everything to any other caller). A call that isn't the one recorded for
        that test (name, file, number of entries) runs whole and the test stays selected."""
        for name in ("load_sql_fixtures", "load_sql_fixture_pairs"):
            original = getattr(mod, name, None)
            if original is None:
                continue

            def loader(filename, _name=name, _original=original):
                plan = entries.get(current["tid"])
                if plan is None:
                    yield from _original(filename)
                    return
                k = current["calls"]
                current["calls"] = k + 1
                items = list(_original(filename))
                calls = plan["calls"]
                if k >= len(calls) or tuple(calls[k]) != (_name, filename, len(items)):
                    failed.add(current["tid"])
                    yield from items
                    return
                for i in plan["keep"].get(k, ()):
                    yield items[i]

            setattr(mod, name, loader)

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
            current["tid"], current["calls"] = getattr(test, "test_id", None), 0

        def stopTest(self, test):
            super().stopTest(test)
            tid = getattr(test, "test_id", None)
            plan = entries.get(tid)
            if plan is not None and current["calls"] != len(plan["calls"]):
                failed.add(tid)  # it didn't load its fixtures the way it did when recorded
            current["tid"] = None
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
        if any(t.startswith(f + "::") for t in entries):
            filter_loaders(mod)
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
                if (f, cls_name) in limits.get("__bare__", ()):
                    # none of these tests reads what setUpClass builds, and the patch can't
                    # change how it runs (see bare_classes): skip it and tearDownClass
                    for case in cases:
                        case.run(result)
                else:
                    unittest.TestSuite(cases).run(result)
            except BaseException:
                failed.update(t for _, t in meths)
            mark(f"ran {cls_name} x{len(cases)}")
    shutil.rmtree(overlay, ignore_errors=True)
    os.write(wfd, json.dumps(sorted(failed)).encode())
    os.close(wfd)


PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
PRELOAD = os.environ.get("BR_PRELOAD", "1") != "0"
# young collections in a check child: tests allocate millions of short-lived nodes, and the
# default threshold collects every 2000 of them (measured: ~5% of a check's CPU)
CHILD_GC = (20000, 20, 20)
HEAVY_FILES = {"tests/test_optimizer.py", "tests/test_executor.py"}  # import duckdb, numpy, pandas


# What `import sqlglot` loads at the pinned commit. A check whose patch changes none of these
# reuses the parent's copy (see preload_sqlglot); any other check imports all of sqlglot again.
CORE_FILES = frozenset((
    "sqlglot/__init__.py", "sqlglot/_typing.py", "sqlglot/_version.py", "sqlglot/dialects/__init__.py",
    "sqlglot/dialects/dialect.py", "sqlglot/diff.py", "sqlglot/errors.py", "sqlglot/expressions/__init__.py",
    "sqlglot/expressions/aggregate.py", "sqlglot/expressions/array.py", "sqlglot/expressions/builders.py",
    "sqlglot/expressions/constraints.py", "sqlglot/expressions/core.py", "sqlglot/expressions/datatypes.py",
    "sqlglot/expressions/ddl.py", "sqlglot/expressions/dml.py", "sqlglot/expressions/functions.py",
    "sqlglot/expressions/json.py", "sqlglot/expressions/math.py", "sqlglot/expressions/properties.py",
    "sqlglot/expressions/query.py", "sqlglot/expressions/string.py", "sqlglot/expressions/temporal.py",
    "sqlglot/generator.py", "sqlglot/helper.py", "sqlglot/jsonpath.py", "sqlglot/optimizer/__init__.py",
    "sqlglot/optimizer/scope.py", "sqlglot/parser.py", "sqlglot/parsers/__init__.py",
    "sqlglot/parsers/base.py", "sqlglot/schema.py", "sqlglot/time.py", "sqlglot/tokenizer_core.py",
    "sqlglot/tokens.py", "sqlglot/trie.py", "sqlglot/typing/__init__.py",
))
_PRELOADED = frozenset()  # sqlglot files the parent imported before forking the checks


def preload_sqlglot():
    """In the parent, before the checks fork: import sqlglot's core once. A child whose patch
    leaves every file of it alone keeps this copy, and the modules it then imports (the
    patched one among them) load through the overlay as before; a child whose patch changes
    one of them forgets every sqlglot module first (enter_overlay)."""
    import importlib

    global _PRELOADED
    if _PRELOADED:
        return  # done already (the import checks preload too)
    src = str(SRC)
    before = set(sys.modules)
    added = src not in sys.path
    if added:
        sys.path.insert(0, src)
    try:
        importlib.import_module("sqlglot")
    except BaseException:
        for name in set(sys.modules) - before:
            if name == "sqlglot" or name.startswith("sqlglot."):
                sys.modules.pop(name, None)
        return
    finally:
        if added:
            sys.path.remove(src)
    loaded = set()
    for name in set(sys.modules) - before:
        path = getattr(sys.modules.get(name), "__file__", None) or ""
        if path.startswith(src + os.sep):
            loaded.add(os.path.relpath(path, src))
    _PRELOADED = frozenset(loaded)


_WARM_STDLIB = []


def warm_stdlib():
    """Import, once in this process, the standard-library modules sqlglot and its tests import,
    so no forked child imports them again; returns their names."""
    import importlib
    import site

    names = ("typing collections re itertools logging datetime decimal functools enum json copy "
             "string inspect gc threading tempfile shutil pathlib multiprocessing multiprocessing.context "
             "concurrent.futures dataclasses contextlib ast unittest unittest.mock difflib heapq csv "
             "sqlite3 importlib.metadata importlib.abc importlib.util select signal zoneinfo uuid hashlib "
             "textwrap pprint operator numbers weakref types pkgutil math time os sys abc builtins "
             "collections.abc argparse traceback warnings").split()
    if not _WARM_STDLIB:
        site.main()
        for name in names:
            try:
                importlib.import_module(name)
            except Exception:
                pass
        _WARM_STDLIB.append(True)
    return names


def warm(jobs):
    """Import, once in the parent, what every check would otherwise import again after forking.
    None of it depends on sqlglot, so the children still load the patched sources."""
    import importlib
    import site

    names = warm_stdlib()
    if any(t.split("::")[0] in HEAVY_FILES for job in jobs
           if "__tpcds__" not in job[3] and "__genmem__" not in job[3] and "__maxnodes__" not in job[3]
           for t in job[2]):
        names += ["numpy", "pandas", "pandas.testing", "duckdb"]
    for name in names:
        try:
            importlib.import_module(name)
        except Exception:
            pass
    if PRELOAD and any(job[1] and not set(job[1]) & CORE_FILES for job in jobs):
        preload_sqlglot()


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


# An ordinary check may use this much CPU, plus the setup allowance of its test files, plus
# four times the recorded time of its tests: an edit can make a test loop for ever (a parser
# that stops advancing), and until the check is stopped that is CPU the run pays for. A normal
# check uses about its recorded time. Its wall-clock limit is looser (CHECK_WALL_S more), since
# on a busy machine a check can wait a long time for the little CPU it needs.
CHECK_BASE_S = 4.0
CHECK_WALL_S = 10.0


def limit_cpu(seconds):
    """In a check child: the kernel ends it (and, each on its own count, the pool workers it
    forks) once it has used this much CPU, which the parent then reads as an unfinished check."""
    import resource
    import signal

    try:
        signal.signal(signal.SIGXCPU, lambda *_: os._exit(4))  # at the soft limit; the hard one kills
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        soft = int(math.ceil(seconds))
        resource.setrlimit(resource.RLIMIT_CPU, (soft, soft + 1))
    except Exception:
        pass


def kill_group(pid):
    """Kill what is left of a check child's process group: the pool workers of a test it was
    killed in the middle of would otherwise keep running, on our CPU time."""
    import signal

    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
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
                    try:
                        os.setpgid(0, 0)  # its own group, so that pool workers it leaves go with it
                    except OSError:
                        pass
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
                    elif "__maxnodes__" in limits:
                        child_maxnodes(patched, limits["__maxnodes__"], w)
                    else:
                        limit_cpu(limits.get("__cpu__", limit))
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
            kill_group(pid)  # a test's process pool can leave workers behind
            log(f"{key}: check used {ru.ru_utime + ru.ru_stime:.2f}s CPU")
            ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
            try:
                results[key] = set(json.loads(buf.decode())) if ok else None
            except ValueError:
                results[key] = None
        now = time.monotonic()
        for pid, (key, fd, until, _) in list(running.items()):
            if now > until or now > deadline:
                kill_group(pid)
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
STATE_BUDGET_S = float(os.environ.get("BR_STATE_BUDGET", "60"))
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
                         "__annotate_func__", "__annotations_cache__",
                         # what a TypedDict records about its keys (`total=`, Required[...]): like the
                         # annotations they are derived from, only typing introspection reads them, and
                         # neither sqlglot nor its tests do any (they are plain dicts at run time)
                         "__total__", "__required_keys__", "__optional_keys__", "__readonly_keys__",
                         "__mutable_keys__"}


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

    def __init__(self, expr_type, masks=(), linemap=None):
        self.memo = {}
        self.keep = []  # visited objects stay alive, so their ids stay unique
        self.expr = expr_type
        self.masks = set(masks)  # (module, class qualname, name): the edited binding itself
        self.linemap = linemap or {}  # path of a patched file -> its change blocks

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
            code = x.__code__
            # the def's line says which of two same-named defs got bound (`if X: def f()...`),
            # in the pristine numbering so lines the patch inserts above it don't count
            line = code.co_firstlineno
            blocks = self.linemap.get(code.co_filename)
            if blocks:
                line = pristine_line(blocks, line)
            out.append(f"func:{x.__module__}.{x.__qualname__}@{line}(")
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


def pristine_line(blocks, n):
    """Line n of a patched file in the pristine numbering, from the patch's change blocks
    ((anchor, removed, added) in order); a line replaced one for one keeps its old number (an
    edited decorator keeps its function's), and one the patch added gets a string."""
    delta = 0
    for anchor, removed, added in blocks:
        start = anchor + delta
        if n < start:
            break
        if n < start + added:
            k = n - start
            return anchor + k if k < removed else f"new{n}"
        delta += added - removed
    return n - delta


def state_import(test_modules):
    """Import every sqlglot module, then the given test modules; the ones that failed to import."""
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
    return failed


def state_digests(failed, masks=(), linemap=None):
    """Per imported sqlglot or test module, a digest of its import-time state (see StateFP),
    with the modules that failed to import."""
    import hashlib

    import sqlglot

    expr = getattr(sqlglot.exp, "Expr", None) or sqlglot.exp.Expression
    fp = StateFP(expr, masks, linemap)
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
    # one digest over all of it: two states are the same when every module's digest and the
    # failed imports are (and a short answer can't fill the pipe it is sent through)
    whole = hashlib.sha256(json.dumps([sorted(digests.items()), failed]).encode()).hexdigest()
    return {"digest": whole, "modules": len(digests), "failed": failed[:40]}


def state_main(args):
    """`selector.py --state <patched pickle> [test modules]`: print the import-time digests of
    the pinned sources with that patch. For the pristine sources (an empty patch) the job
    lists every set of masked bindings its commits need ("mask_sets"): the import is done
    once and each set is fingerprinted in a fork of that state, so one fingerprint can't
    leave anything behind for the next."""
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

    def forked(mask_set, failed):
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(r)
                os.write(w, json.dumps(state_digests(failed, mask_set, {})).encode())
            finally:
                os._exit(0)
        os.close(w)
        chunks = []
        while True:
            chunk = os.read(r, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(r)
        os.waitpid(pid, 0)
        try:
            return json.loads(b"".join(chunks).decode())
        except ValueError:
            return None

    def run():
        try:
            linemap = {os.path.join(str(SRC), rel): blocks for rel, blocks in job.get("blocks", {}).items()}
            failed = state_import(test_modules)
            if "mask_sets" in job:
                result["states"] = [forked([tuple(m) for m in mask_set], failed) for mask_set in job["mask_sets"]]
            else:
                result["state"] = state_digests(failed, masks, linemap)
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
    """jobs: {key: (patched, test modules, masked bindings, change blocks)}. Runs the fingerprint
    for each, plus the pristine sources once per set of test modules (one process imports
    them and fingerprints every set of masked bindings its commits need), two at a time;
    returns {key: True if every digest matches the pristine one (and no import fails that
    didn't before)}."""
    import subprocess
    import tempfile
    import time

    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    runs, pristine = {}, {}
    for key, (patched, mods, masks, blocks) in jobs.items():
        mods, masks = tuple(mods), tuple(sorted(masks))
        runs[key] = (patched, mods, masks, blocks)
        sets = pristine.setdefault(("pristine", mods), [])
        if masks not in sets:
            sets.append(masks)
    tmp = tempfile.mkdtemp(prefix="br-state-")
    pending = [(key, ({}, key[1], sets, {})) for key, sets in pristine.items()] + list(runs.items())
    running, outputs = {}, {}
    deadline = time.monotonic() + budget_s
    started = 0
    try:
        while pending or running:
            while pending and len(running) < PARALLEL and time.monotonic() < deadline:
                key, (patched, mods, masks, blocks) = pending.pop(0)
                started += 1
                path = os.path.join(tmp, f"job{started}.pkl")  # one file per job
                job = {"patched": patched, "blocks": blocks}
                if key in pristine:
                    job["mask_sets"] = [list(m) for m in masks]
                else:
                    job["masks"] = list(masks)
                with open(path, "wb") as fh:
                    pickle.dump(job, fh)
                proc = subprocess.Popen([sys.executable, "-S", str(Path(__file__).resolve()), "--state", path, *mods],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=tmp,
                                        preexec_fn=_state_limits)
                # the pristine process fingerprints its sets one after the other (about 0.5 s each)
                running[key] = (proc, time.monotonic() + STATE_WALL_S + (3 * len(masks) if key in pristine else 0))
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
                    got = json.loads(out.decode())
                except ValueError:
                    got = {}
                if key in pristine:
                    for masks, state in zip(pristine[key], got.get("states") or ()):
                        outputs[(key[1], masks)] = state
                else:
                    outputs[key] = got.get("state")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    verdict = {}
    for key, (_, mods, masks, _blocks) in jobs.items():
        mine, base = outputs.get(key), outputs.get((tuple(mods), tuple(sorted(masks))))
        if mine and base:
            mine_failed, base_failed = mine.pop("failed", None) or [], base.get("failed") or []
            mine_same = dict(mine) == {k: v for k, v in base.items() if k != "failed"}
            # an import that no longer succeeds is more than changed state: see import_checks
            verdict[key] = "broken" if set(mine_failed) - set(base_failed) else bool(mine_same)
            mine["failed"] = mine_failed
        else:
            verdict[key] = None  # no answer (out of time, killed, crashed): not known either way
    return verdict


# An edit that runs while modules are imported (class and module level statements, function
# lines the imports run) can make an import fail. Then every test of a test file that imports
# the module fails at collection, and every test that loads it later (a dialect by name) fails
# when it does, whatever lines those tests ran at the pinned commit: coverage of the edited
# line says nothing about them. So such commits are imported for real: a forked child with the
# patched sources imports sqlglot, every sqlglot module, every dialect by name and the test
# modules whose import runs sqlglot code. Anything that fails and didn't before (the pinned
# sources are imported the same way, once, only when some commit reports a failure), or a
# child that crashes or runs out of time, selects every test.
IMPORT_CHECK = os.environ.get("BR_IMPORT_CHECK", "1") != "0"
IMPORT_CPU_S = 6.0  # all of it takes about 0.3 s; an edit can make an import-time parse loop
IMPORT_WALL_S = 25.0


def import_failures(test_modules):
    """Import sqlglot, every sqlglot module, every dialect by name and the given test modules;
    the ones that raise, with the exception's type."""
    import importlib
    import pkgutil

    failed = []
    try:
        import sqlglot
    except BaseException as error:  # noqa: BLE001
        return [f"sqlglot: {type(error).__name__}"]
    found = []
    try:
        found = sorted(m.name for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot.",
                                                             onerror=lambda name: failed.append(f"{name}: package"))
                       if not m.name.endswith("__main__"))
    except BaseException as error:  # noqa: BLE001
        failed.append(f"walk: {type(error).__name__}")
    for name in found:
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


def child_imports(patched, test_modules, wfd):
    """In a forked child: what fails to import with the patched sources (import_failures)."""
    import shutil

    limit_cpu(IMPORT_CPU_S)
    limit_memory()
    # as in the checks: sqlglot's core, imported once by the parent from the pinned sources,
    # is kept when the patch changes none of its files (enter_overlay forgets it otherwise);
    # everything else is imported here, the patched files through the overlay
    overlay = enter_overlay(patched)
    for name in [n for n in sys.modules if n.split(".")[0] == "tests"]:
        del sys.modules[name]
    failed = import_failures(test_modules)
    shutil.rmtree(overlay, ignore_errors=True)
    os.write(wfd, json.dumps(failed).encode())
    os.close(wfd)


def import_checks(jobs, budget_s):
    """jobs: {commit: patched sources}. Returns {commit: True if an import newly fails (or the
    child gave no answer), False if all is as with the pinned sources, None if it wasn't run}."""
    import select
    import time

    if not jobs:
        return {}
    warm_stdlib()  # with site-packages, as in the checks: the test modules import from there
    if PRELOAD and sum(1 for patched in jobs.values() if patched and not set(patched) & CORE_FILES) >= 2:
        preload_sqlglot()
    import gc
    gc.freeze()  # the children are forks: keep their collector off the maps loaded here
    mods = tuple((load_imports() or {}).get("test_modules", ()))
    deadline = time.monotonic() + budget_s
    got = {}
    used = [0.0]

    def run(batch):
        pending, running = list(batch), {}
        while pending or running:
            while pending and len(running) < PARALLEL and time.monotonic() < deadline:
                key, patched = pending.pop(0)
                r, w = os.pipe()
                sys.stdout.flush()
                sys.stderr.flush()
                pid = os.fork()
                if pid == 0:
                    try:
                        try:
                            os.setpgid(0, 0)
                        except OSError:
                            pass
                        os.close(r)
                        devnull = os.open(os.devnull, os.O_WRONLY)
                        os.dup2(devnull, 1)
                        if not os.environ.get("BR_DEBUG"):
                            os.dup2(devnull, 2)
                        child_imports(patched, mods, w)
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
                _, status, ru = os.wait4(pid, 0)
                kill_group(pid)
                used[0] += ru.ru_utime + ru.ru_stime
                try:
                    got[key] = json.loads(buf.decode()) if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0 else None
                except ValueError:
                    got[key] = None
                got[key] = ("done", got[key])
            now = time.monotonic()
            for pid, (key, fd, until, _) in list(running.items()):
                if now > until:
                    kill_group(pid)
                    try:
                        os.kill(pid, 9)
                    except OSError:
                        pass
                    os.close(fd)
                    os.wait4(pid, 0)
                    running.pop(pid)
                    got[key] = ("done", None)

    run(list(jobs.items()))
    suspects = [k for k, v in got.items() if v[1] is None or v[1]]
    base = []
    if suspects:
        run([("__pinned__", {})])
        base = (got.pop("__pinned__", None) or (None, None))[1]
        if base is None:
            base = []  # the pinned sources import cleanly; without an answer assume they did
    log(f"import check of {len(jobs)} commits used {used[0]:.2f}s CPU")
    verdict = {}
    for key in jobs:
        if key not in got:
            verdict[key] = None
        else:
            failed = got[key][1]
            verdict[key] = failed is None or bool(set(failed) - set(base))
            if verdict[key]:
                log(f"{key}: imports fail with the patch: {'no answer' if failed is None else sorted(set(failed) - set(base))[:4]}")
    return verdict


def needs_import_check(selector, snap, every):
    """Whether a commit's edit can change what importing does: it changes sqlglot code at
    class or module level, or a function line that runs while modules are imported, and its
    selection is short of every test. Not for an edit confined to the body of a lambda that
    no import runs (the statement builds the same objects), nor for the literal value of a
    class constant that no import-time code reads ("constant", owner_reads_mask)."""
    if len(snap["chosen"]) >= len(every) or not snap["patched"]:
        return False
    for rel, lo, hi, kind in snap["touched"]:
        if rel.startswith("sqlglot/") and rel.endswith(".py") and rel in snap["patched"]:
            if kind in ("lambda", "constant"):
                continue
            if kind != "function" or selector.touches_import(rel, lo, hi):
                return True
    return False


def phase(name):
    """With BR_PHASES set: log this process's CPU so far and that of the children it has reaped."""
    if os.environ.get("BR_PHASES"):
        import resource
        me, ch = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
        log(f"PHASE {name}: self {me.ru_utime + me.ru_stime:.3f}s children {ch.ru_utime + ch.ru_stime:.3f}s")


# A run that dies or runs out of time prints nothing and is rejected, so the last resort is an
# answer for every commit from what is proven by then. The watchdog fires well inside the three
# minutes a practice run gets (the checks' own budget ends earlier).
WATCHDOG_S = int(os.environ.get("BR_WATCHDOG", "165"))
_GUARD = {"pid": None, "duration": None, "every": None, "answers": None, "written": False}


def emergency_output(reason):
    """Print an answer for every commit and leave: a commit's selection once its import check is
    done, less what a check saw pass (main's `answers`, sound at every moment), and every test
    for a commit that isn't that far yet."""
    if os.getpid() != _GUARD["pid"]:
        os._exit(3)  # a forked child never answers
    if _GUARD["written"]:
        return
    _GUARD["written"] = True
    try:
        try:
            log(f"emergency output: {reason!r}")
        except Exception:
            pass
        duration, every, answers = _GUARD["duration"], _GUARD["every"], _GUARD["answers"] or {}
        if every is None:
            tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
            duration = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
            every = sorted(duration)
        lines = []
        for patch in sorted((DATA / "commits").glob("*.patch")):
            chosen = list(answers.get(patch.name[: -len(".patch")], every))
            try:
                workers = pack(chosen, duration)
            except Exception:
                workers = [chosen[i::MAX_WORKERS] for i in range(MAX_WORKERS) if chosen[i::MAX_WORKERS]]
            lines.append(json.dumps({"commit": patch.name[: -len(".patch")], "workers": workers}, separators=(",", ":")))
        sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
        sys.stdout.flush()
        stop_children()
    finally:
        os._exit(0)


def stop_children():
    """Kill the checks still running (each sits in a process group of its own)."""
    me = os.getpid()
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat") as fh:
                    parent = int(fh.read().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            if parent == me:
                for kill in (os.killpg, os.kill):
                    try:
                        kill(int(entry), 9)
                    except OSError:
                        pass
    except OSError:
        pass


def cli():
    """main() under the guard: the watchdog, and the emergency output if anything raises."""
    _GUARD["pid"] = os.getpid()
    try:
        import signal
        signal.signal(signal.SIGALRM, lambda *_: emergency_output("out of time"))
        signal.alarm(WATCHDOG_S)
    except Exception:
        pass
    try:
        main()
    except BaseException as error:
        emergency_output(error)
    try:
        sys.stdout.flush()
    finally:
        os._exit(0)


def main():
    import time
    started = time.monotonic()
    phase("start")
    tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
    duration = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
    every = sorted(duration)
    _GUARD["duration"], _GUARD["every"] = duration, every
    number_of = {t["id"]: t["number"] for t in tests}
    id_of = {t["number"]: t["id"] for t in tests}
    info = {}
    for t in tests:
        parts = t["id"].split("::")
        info[t["number"]] = {
            "file": t["file"],
            "runnable": (len(parts) == 3 and "[" not in t["id"] and t["file"] not in SKIP_FILES
                         and t["id"] not in SKIP_TESTS and duration[t["number"]] <= MAX_TEST_MS),
            "extra": EXTRA_CPU_MS.get(t["id"], 0),
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
                "state_masks": list(selector.state_masks), "line_blocks": dict(selector.line_blocks)}

    phase("maps loaded")
    # 1. selections, and the commits whose selection an unchanged import-time state would shrink
    snaps, narrow, state_jobs = {}, {}, {}
    for patch in sorted((DATA / "commits").glob("*.patch")):
        commit = patch.name[: -len(".patch")]
        text = ""
        try:
            text = patch.read_bytes().decode("utf-8", "surrogateescape")
            if selector is None:
                raise Fallback("no map")
            snap = run_select(text)
        except Exception as error:
            log(f"{commit}: running every test ({error})")
            snap = {"chosen": every, "patched": {}, "verifiable": False, "unreadable": set(), "touched": [],
                    "data_files": set(), "import_widened": False, "state_masks": [], "line_blocks": {}}
        snaps[commit] = snap
        # (a data file changed next to the code doesn't matter here: one that anything reads
        # outside tests is a config file and ran everything above; the others reach only the
        # tests that open them, which both selections hold and no check runs)
        if RUN and snap["verifiable"] and snap["import_widened"] and snap["patched"]:
            try:
                alt = run_select(text, widen=False)
            except Exception as error:
                log(f"{commit}: no narrower selection ({error})")
                alt = None
            gain = None
            if alt is not None:
                # What each selection should cost once its own run check has dropped what
                # passes: a wide selection that is cheap to run doesn't need the state check.
                gain = (pack_cost(snap["chosen"], duration)
                        - plan_check(snap["chosen"], duration, info, snap["unreadable"], want_gain=True)
                        - pack_cost(alt["chosen"], duration)
                        + (plan_check(alt["chosen"], duration, info, alt["unreadable"], want_gain=True)
                           if alt["verifiable"] else 0))
                tpcds_number = number_of.get(TPCDS_TEST)
                if tpcds_number in alt["chosen"] and gain < STATE_MIN_GAIN_MS:
                    # as ordinary function code the commit may also get a TPC-DS replay
                    selector.patched, selector.verifiable = alt["patched"], alt["verifiable"]
                    selector.touched, selector.data_files = alt["touched"], alt["data_files"]
                    if selector.tpcds_plan(alt["chosen"], duration, tpcds_number, alt["unreadable"]):
                        gain += (pack_cost(alt["chosen"], duration)
                                 - pack_cost([n for n in alt["chosen"] if n != tpcds_number], duration)) // 2
            if gain is not None and gain >= STATE_MIN_GAIN_MS:
                narrow[commit] = alt
                state_jobs[commit] = (snap["patched"], tuple((load_imports() or {}).get("test_modules", ())),
                                      tuple(alt["state_masks"]), snap["line_blocks"])
    phase("selected")
    everything = {"chosen": every, "patched": {}, "verifiable": False, "unreadable": set(), "touched": [],
                  "data_files": set(), "import_widened": False, "state_masks": [], "line_blocks": {}}
    imports_known = set()  # commits whose patched imports were seen to succeed as the pinned ones do
    if state_jobs:
        verdict = import_states(state_jobs, max(5.0, STATE_BUDGET_S))
        for commit, same in sorted(verdict.items()):
            if same == "broken":
                log(f"{commit}: imports fail with the patch (state check): running every test")
                snaps[commit] = dict(everything)
                continue
            log(f"{commit}: import-time state {'unchanged' if same else 'changed or unknown'}")
            if same:
                snaps[commit] = narrow[commit]
            if same is not None:
                imports_known.add(commit)
    phase("state checks")
    if IMPORT_CHECK and RUN and selector is not None:
        imp_jobs = {commit: snap["patched"] for commit, snap in snaps.items()
                    if commit not in imports_known and needs_import_check(selector, snap, every)}
        if imp_jobs:
            budget = float(os.environ.get("BR_BUDGET", "140")) - (time.monotonic() - started)
            verdict = import_checks(imp_jobs, max(5.0, min(40.0, budget - 60)))
            for commit, broken in sorted(verdict.items()):
                if broken or broken is None:
                    # an import fails, or there was no time to find out: every test
                    if broken is None:
                        log(f"{commit}: no time for the import check: running every test")
                    snaps[commit] = dict(everything)

    phase("import checks")
    # 2. the checks each selection is worth
    answers = {}
    _GUARD["answers"] = answers  # from here on a commit's entry is sound whenever it exists
    jobs, tpcds_jobs = [], []
    tpcds_number = number_of.get(TPCDS_TEST)
    genmem_number = number_of.get(GENMEM_TEST)
    maxnodes_number = number_of.get(MAXNODES_TEST)
    for commit, snap in snaps.items():
        chosen, patched, verifiable, unreadable = snap["chosen"], snap["patched"], snap["verifiable"], snap["unreadable"]
        if selector is not None:
            selector.patched, selector.verifiable = patched, verifiable
            selector.touched, selector.data_files = snap["touched"], snap["data_files"]
        answers[commit] = chosen
        if RUN and verifiable and selector is not None and tpcds_number in chosen:
            plan = selector.tpcds_plan(chosen, duration, tpcds_number, unreadable)
            if plan:
                tpcds_jobs.append(((commit, "tpcds"), patched, [TPCDS_TEST],
                                   {"__tpcds__": plan, TPCDS_TEST: duration[tpcds_number] / 1000},
                                   10 + max(3 * plan["estimate"], 1.5 * plan["budget"])))
        if RUN and verifiable and patched and maxnodes_number in chosen and maxnodes_number not in unreadable \
                and all(rel.endswith(".py") and rel.startswith(("sqlglot/", "tests/")) and rel != "tests/test_parser.py"
                        for rel in patched):
            # every changed source is overlaid and the test's own module is as recorded
            saving = (pack_cost(chosen, duration)
                      - pack_cost([n for n in chosen if n != maxnodes_number], duration)) / 1000
            if MAXNODES_P_PASS * saving > 1.5 * MAXNODES_CPU_S:
                tpcds_jobs.append(((commit, "maxnodes"), patched, [MAXNODES_TEST],
                                   {"__maxnodes__": {"budget": MAXNODES_BUDGET_S},
                                    MAXNODES_TEST: duration[maxnodes_number] / 1000}, 10 + MAXNODES_BUDGET_S))
        if RUN and verifiable and selector is not None and genmem_number in chosen:
            gplan = selector.genmem_plan(chosen, duration, genmem_number, unreadable)
            if gplan is not None and not gplan["dialects"] and not gplan["import_check"]:
                chosen = [n for n in chosen if n != genmem_number]  # no dialect loads what changed
                answers[commit] = chosen
            elif gplan is not None:
                tpcds_jobs.append(((commit, "genmem"), patched, [GENMEM_TEST],
                                   {"__genmem__": gplan, GENMEM_TEST: duration[genmem_number] / 1000},
                                   10 + 3 * gplan["estimate"]))
        if RUN and verifiable and chosen:
            bare = selector is not None and selector.bare_ok()
            replay = {}  # fixture-driven tests that only need a few of their entries checked
            if selector is not None and selector.plain_function_commit():
                for n in chosen:
                    if info[n]["file"] in FIXTURE_FILES and info[n]["runnable"] and n not in unreadable:
                        got = selector.fixture_entries(id_of[n])
                        if got is not None and got[1] <= FIXTURE_MAX_SHARE:
                            replay[n] = (got[0], FIXTURE_SETUP_MS + FIXTURE_MARGIN * got[1] * got[2])
            check = plan_check(chosen, duration, info, unreadable, bare=bare,
                               cost_ms={n: ms for n, (_, ms) in replay.items()})
            if check:
                expected = sum(duration[n] for n in check) / 1000
                limits = {id_of[n]: duration[n] / 1000 for n in check}
                if bare:
                    limits["__bare__"] = bare_classes(list(limits))
                bare_files = {f for f, _ in limits.get("__bare__", ())}
                setup = sum(BARE_FILE_OVERHEAD_MS if f in bare_files else FILE_OVERHEAD_MS.get(f, DEFAULT_FILE_OVERHEAD_MS)
                            for f in {info[n]["file"] for n in check}) / 1000
                limits["__cpu__"] = CHECK_BASE_S + setup + 4 * expected
                limit = limits["__cpu__"] + CHECK_WALL_S
                entries = {id_of[n]: replay[n][0] for n in check if n in replay}
                if entries:
                    limits["__entries__"] = entries
                    log(f"{commit}: " + ", ".join(
                        f"{t.split('::')[-1]} on {sum(len(v) for v in e['keep'].values())} of "
                        f"{sum(c[2] for c in e['calls'])} entries" for t, e in sorted(entries.items())))
                jobs.append((commit, patched, [id_of[n] for n in check], limits, limit))
        log(f"{commit}: {len(chosen)} tests")

    phase("checks planned")
    jobs = tpcds_jobs + jobs
    if jobs:
        budget = float(os.environ.get("BR_BUDGET", "140")) - (time.monotonic() - started)
        warm(jobs)
        phase("warm")
        # every check is a fork of this process: keep the collector in each child away from
        # the maps and imports already here (walking them would also copy their pages)
        import gc
        gc.freeze()
        results = run_checks(jobs, budget)
        for key, _, test_ids, _, _ in jobs:
            commit = key[0] if isinstance(key, tuple) else key
            failed = results.get(key)
            if failed is None:
                continue
            passed = {number_of[t] for t in test_ids if t not in failed}
            log(f"{commit}: ran {len(test_ids)}, {len(test_ids) - len(passed)} failed")
            answers[commit] = [n for n in answers[commit] if n not in passed]

    phase("checks run")
    out = [json.dumps({"commit": c, "workers": pack(answers[c], duration)}, separators=(",", ":"))
           for c in answers]
    _GUARD["written"] = True
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--state":
        state_main(sys.argv[2:])
    else:
        cli()
