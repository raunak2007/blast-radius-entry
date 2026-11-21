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

    def classes(self):
        out = []
        for st in self.stmts:
            if st["kind"] == "class" and st["scope"] in ("module", "class"):
                node = st["node"]
                out.append(f"{st['cls']}.{node.name}" if st["cls"] and st["scope"] == "class" else node.name)
        return out


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

    def mask_for_old_line(self, rel, idx, line):
        st, span = idx.stmt_at(line)
        if st is None:
            return self.module_fallback(rel, idx)
        lo, hi = span
        if st["scope"] == "function":
            m = self.line_union(rel, lo, hi)
            if self.touches_import(rel, lo, hi):
                func = st["func"]
                m |= self.broad_for_function(rel, idx, func)
            return m
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
            return self.class_fallback(rel, st["cls"], idx)
        return self.module_fallback(rel, idx, st["node"])

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
        try:
            new_idx = SourceIndex(new_text)
        except SyntaxError:
            raise Fallback(f"syntax error in {rel}")
        m = 0
        for blk in blocks:
            if self.comment_only(blk, idx, new_idx):
                continue
            for ln in blk["removed"]:
                m |= self.mask_for_old_line(rel, idx, ln)
            if blk["added"]:
                m |= self.mask_for_added(rel, idx, new_idx, blk)
        return m

    def mask_for_added(self, rel, idx, new_idx, blk):
        """Added lines inside a function run where the lines around them ran. A new or
        rewritten class attribute, method or class changes the class; a new module
        statement changes the module."""
        old_defs = {(st["cls"], st["node"].name) for st in idx.stmts if st["kind"] in ("def", "class")}
        for ln in blk["added"]:
            st, _ = new_idx.stmt_at(ln)
            if st is None or st["scope"] == "function":
                continue
            if st["kind"] in ("def", "class") and blk["removed"] and (st["cls"], st["node"].name) in old_defs:
                continue  # an existing header was edited; its removed lines already count
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
            return file_mask
        new_text, blocks = apply_hunks(old_text, fd["hunks"])
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
            if fd["new_file"]:
                continue  # a new file nothing imports or opens can't change a test
            if rel.endswith(".py"):
                if not fd["hunks"]:
                    raise Fallback(f"no hunks for {rel}")
                if rel.startswith("sqlglot/"):
                    m |= self.python_source(rel, fd)
                elif rel.startswith("tests/"):
                    m |= self.test_source(rel, fd)
                else:
                    m |= doc["test_files"].get("tests/test_sqlglotc.py", 0) | doc["test_files"].get("tests/test_build.py", 0)
        return m


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


def main():
    tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
    duration = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
    every = sorted(duration)
    number_of = {t["id"]: t["number"] for t in tests}

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

    out = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        commit = patch.name[: -len(".patch")]
        try:
            if selector is None:
                raise Fallback("no map")
            mask = selector.select(patch.read_bytes().decode("utf-8", "surrogateescape"))
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
        out.append(json.dumps({"commit": commit, "workers": pack(chosen, duration)}, separators=(",", ":")))
        log(f"{commit}: {len(chosen)} tests")
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


if __name__ == "__main__":
    main()
