"""Generate a validation set of patches against the pinned sqlglot (dev tool).

Kinds, loosely modelled on the sample commits plus kinds the final may add:
  code      one small semantic edit in sqlglot/*.py (operator/constant/boolean swaps,
            deleted statement, changed argument, ...), function bodies and class bodies
  comment   a comment line added to a .py file or a fixture
  fixture   a fixture line edited (token injected/removed, expected output changed)
  test      a test file edited (assertion literal changed, or comment)
  docstring a docstring edited
  multi     2-3 of the above in different files
  newfile   a new file added (module, fixture or test file)
  delete    a file deleted
  syntax    an edit that breaks parsing

Ground truth is computed separately (tools/ground_truth.py). Nothing here is
used by the selector at decision time.

Usage: python tools/gen_mutants.py <sqlglot checkout> <out dir> <count> [seed]
"""

from __future__ import annotations

import ast
import difflib
import json
import os
import random
import re
import sys

SWAPS = [
    (r" == ", " != "), (r" != ", " == "), (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "),
    (r" >= ", " > "), (r" is not ", " is "), (r" is None", " is not None"), (r" and ", " or "),
    (r" or ", " and "), (r"\bnot ", ""), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
    (r" in ", " not in "), (r" \+ ", " - "), (r" - ", " + "), (r"\[0\]", "[1]"), (r"\[1\]", "[0]"),
    (r"\[-1\]", "[0]"), (r"copy=False", "copy=True"), (r"copy=True", "copy=False"),
    (r"\bif ", "if not "), (r"\breturn True\b", "return False"), (r"\breturn False\b", "return True"),
    (r" is ", " == "), (r"\bNone\b", "0"), (r"\bbreak\b", "continue"), (r"\bmin\(", "max("),
    (r"\bmax\(", "min("), (r"\bany\(", "all("), (r"\ball\(", "any("),
]
# The operators seen in the kit sample's code commits (--official): identity vs
# equality, ==/!=, comparison boundaries, and/or, added/removed `not`, booleans,
# +-1 on integers (also in class bodies and dict values), copy= flips.
OFFICIAL_SWAPS = [
    (r" == ", " != "), (r" != ", " == "), (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "),
    (r" >= ", " > "), (r" is not ", " is "), (r" is None", " == None"), (r" is not None", " != None"),
    (r" is ", " == "), (r" and ", " or "), (r" or ", " and "), (r"\bnot ", ""), (r"\bif ", "if not "),
    (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"copy=False", "copy=True"), (r"copy=True", "copy=False"),
]
NUM = re.compile(r"(?<![\w.])(\d+)(?![\w.])")
STR = re.compile(r"\"([A-Za-z_ ]{2,30})\"")
COMMENTS = ["# Cheap check first", "# Kept for backwards compatibility", "# TODO: revisit",
            "# Mirrors the behavior of the reference implementation", "# noqa", "# type: ignore"]


def git_diff(rel, old, new, new_file=False, deleted=False):
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    out = [f"diff --git a/{rel} b/{rel}\n"]
    if new_file:
        out.append("new file mode 100644\n")
    if deleted:
        out.append("deleted file mode 100644\n")
    out.append("index 0000000..1111111 100644\n" if not (new_file or deleted) else "index 0000000..1111111\n")
    lines = list(difflib.unified_diff(a, b, "/dev/null" if new_file else f"a/{rel}", "/dev/null" if deleted else f"b/{rel}", n=3))
    for line in lines:
        if not line.endswith("\n"):
            line += "\n\\ No newline at end of file\n"
        out.append(line)
    return "".join(out)


class Gen:
    def __init__(self, repo, rng):
        self.repo = repo
        self.rng = rng
        self.py = []
        for base in ("sqlglot",):
            for d, dn, fn in os.walk(os.path.join(repo, base)):
                dn[:] = [x for x in dn if x != "__pycache__"]
                for f in fn:
                    if f.endswith(".py"):
                        self.py.append(os.path.relpath(os.path.join(d, f), repo))
        self.tests = []
        for d, dn, fn in os.walk(os.path.join(repo, "tests")):
            dn[:] = [x for x in dn if x not in ("__pycache__", "fixtures")]
            for f in fn:
                if f.startswith("test_") and f.endswith(".py"):
                    self.tests.append(os.path.relpath(os.path.join(d, f), repo))
        self.fixtures = []
        for d, dn, fn in os.walk(os.path.join(repo, "tests", "fixtures")):
            for f in fn:
                if f.endswith(".sql"):
                    self.fixtures.append(os.path.relpath(os.path.join(d, f), repo))
        self.py.sort(); self.tests.sort(); self.fixtures.sort()
        self._weights = [max(1, os.path.getsize(os.path.join(repo, p)) // 1000) for p in self.py]

    def read(self, rel):
        return open(os.path.join(self.repo, rel), encoding="utf-8").read()

    def dump_of(self, rel, src):
        cache = self.__dict__.setdefault("_dumps", {})
        if rel not in cache:
            cache[rel] = ast.dump(ast.parse(src))
        return cache[rel]

    def pick_py(self):
        if getattr(self, "uniform", False):
            return self.rng.choice([p for p in self.py if os.path.getsize(os.path.join(self.repo, p)) > 200])
        return self.rng.choices(self.py, weights=self._weights)[0]

    # -- edits -------------------------------------------------------------
    def code_edit(self, rel=None, tries=200):
        for _ in range(tries):
            rel = rel or self.pick_py()
            src = self.read(rel)
            lines = src.split("\n")
            i = self.rng.randrange(len(lines))
            line = lines[i]
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith(('"""', "'''")):
                rel = None
                continue
            options = []
            official = getattr(self, "official", False)
            for pat, rep in (OFFICIAL_SWAPS if official else SWAPS):
                for m in re.finditer(pat, line):
                    options.append((m.start(), m.end(), rep))
            for m in NUM.finditer(line):
                n = int(m.group(1))
                options.append((m.start(), m.end(), str(n + self.rng.choice([1, -1]) if n > 0 else 1)))
            if not official:
                for m in STR.finditer(line):
                    options.append((m.start(1), m.end(1), m.group(1) + "X"))
            if not official and self.rng.random() < 0.12 and not stripped.endswith((":", "(", "[", "{", ",")) and not stripped.startswith(("def ", "class ", "@", "return", "elif", "else", "except", "finally")):
                options.append(("delete", None, None))
            if not options:
                rel = None
                continue
            start, end, rep = self.rng.choice(options)
            if start == "delete":
                indent = line[: len(line) - len(line.lstrip())]
                new_line = indent + "pass"
            else:
                new_line = line[:start] + rep + line[end:]
            if new_line == line:
                rel = None
                continue
            new_lines = list(lines)
            new_lines[i] = new_line
            new = "\n".join(new_lines)
            try:
                ast.parse(new)
            except SyntaxError:
                rel = None
                continue
            if self.dump_of(rel, src) == ast.dump(ast.parse(new)):
                rel = None
                continue
            return rel, src, new, f"{rel}:{i + 1} {line.strip()[:60]!r} -> {new_line.strip()[:60]!r}"
        raise RuntimeError("no code edit found")

    def classlevel_edit(self, tries=300):
        """Edit a constant inside a class-body or module-level statement (import time)."""
        for _ in range(tries):
            rel = self.pick_py()
            src = self.read(rel)
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            spans = []

            def walk(body, in_class):
                for node in body:
                    if isinstance(node, ast.ClassDef):
                        walk(node.body, True)
                    elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                        spans.append((node.lineno, node.end_lineno))
            walk(tree.body, False)
            if not spans:
                continue
            start, end = self.rng.choice(spans)
            lines = src.split("\n")
            i = self.rng.randrange(start - 1, end)
            line = lines[i]
            options = []
            for pat, rep in [(r"\bTrue\b", "False"), (r"\bFalse\b", "True")]:
                for m in re.finditer(pat, line):
                    options.append((m.start(), m.end(), rep))
            for m in NUM.finditer(line):
                options.append((m.start(), m.end(), str(int(m.group(1)) + 1)))
            for m in STR.finditer(line):
                options.append((m.start(1), m.end(1), m.group(1) + "X"))
            if not options:
                continue
            a, b, rep = self.rng.choice(options)
            new_lines = list(lines)
            new_lines[i] = line[:a] + rep + line[b:]
            new = "\n".join(new_lines)
            try:
                if self.dump_of(rel, src) == ast.dump(ast.parse(new)):
                    continue
            except SyntaxError:
                continue
            return rel, src, new, f"{rel}:{i + 1} classlevel {line.strip()[:60]!r}"
        raise RuntimeError("no class-level edit")

    def comment_edit(self, rel=None):
        rel = rel or (self.pick_py() if self.rng.random() < 0.8 else self.rng.choice(self.fixtures))
        src = self.read(rel)
        lines = src.split("\n")
        while len(lines) < 5:
            rel = self.pick_py()
            src = self.read(rel)
            lines = src.split("\n")
        for _ in range(100):
            i = self.rng.randrange(1, len(lines))
            line = lines[i]
            if rel.endswith(".py"):
                if not line.strip():
                    continue
                indent = line[: len(line) - len(line.lstrip())]
                comment = self.rng.choice(COMMENTS)
                new_lines = lines[:i] + [indent + comment] + lines[i:]
                new = "\n".join(new_lines)
                try:
                    ast.parse(new)
                except SyntaxError:
                    continue
                return rel, src, new, f"{rel}:{i + 1} comment"
            else:
                comment = self.rng.choice(["-- note", "-- " + self.rng.choice(COMMENTS)[2:], "# note: x", "#" + self.rng.choice(COMMENTS)[1:]])
                new_lines = lines[:i] + [comment] + lines[i:]
                return rel, src, "\n".join(new_lines), f"{rel}:{i + 1} fixture comment {comment!r}"
        raise RuntimeError("no comment spot")

    def fixture_edit(self, rel=None):
        rel = rel or self.rng.choice(self.fixtures)
        src = self.read(rel)
        lines = src.split("\n")
        for _ in range(200):
            i = self.rng.randrange(len(lines))
            line = lines[i]
            if not line.strip() or line.startswith("--") or line.startswith("#"):
                continue
            words = line.split(" ")
            choice = self.rng.random()
            if choice < 0.4:
                k = self.rng.randrange(len(words) + 1)
                words.insert(k, self.rng.choice(["42", "x", "0", "NULL", "1"]))
                new_line = " ".join(words)
            elif choice < 0.6 and len(words) > 2:
                k = self.rng.randrange(len(words))
                del words[k]
                new_line = " ".join(words)
            elif choice < 0.8:
                k = self.rng.randrange(len(line))
                new_line = line[:k] + line[k + 1:]
            else:
                new_line = line.replace(" ", "  ", 1) if self.rng.random() < 0.5 else line + " "
            if new_line == line:
                continue
            new_lines = list(lines)
            new_lines[i] = new_line
            return rel, src, "\n".join(new_lines), f"{rel}:{i + 1} {line[:50]!r} -> {new_line[:50]!r}"
        raise RuntimeError("no fixture edit")

    def test_edit(self):
        rel = self.rng.choice(self.tests)
        src = self.read(rel)
        lines = src.split("\n")
        for _ in range(200):
            i = self.rng.randrange(len(lines))
            line = lines[i]
            m = list(re.finditer(r"\"([^\"\\\\]{3,60})\"", line))
            if not m:
                continue
            mm = self.rng.choice(m)
            text = mm.group(1)
            k = self.rng.randrange(len(text))
            new_text = text[:k] + self.rng.choice(["X", " ", ""]) + text[k + 1:]
            if new_text == text:
                continue
            new_line = line[: mm.start(1)] + new_text + line[mm.end(1):]
            new_lines = list(lines)
            new_lines[i] = new_line
            new = "\n".join(new_lines)
            try:
                ast.parse(new)
            except SyntaxError:
                continue
            return rel, src, new, f"{rel}:{i + 1} test literal {text[:40]!r} -> {new_text[:40]!r}"
        raise RuntimeError("no test edit")

    def docstring_edit(self):
        for _ in range(200):
            rel = self.pick_py()
            src = self.read(rel)
            tree = ast.parse(src)
            docs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Module)) and ast.get_docstring(n)]
            if not docs:
                continue
            node = self.rng.choice(docs)
            expr = node.body[0]
            lines = src.split("\n")
            i = expr.lineno - 1
            line = lines[i]
            words = re.finditer(r"[A-Za-z]{4,}", line)
            words = list(words)
            if not words:
                continue
            w = self.rng.choice(words)
            new_line = line[: w.start()] + w.group(0)[::-1] + line[w.end():]
            new_lines = list(lines)
            new_lines[i] = new_line
            new = "\n".join(new_lines)
            try:
                ast.parse(new)
            except SyntaxError:
                continue
            return rel, src, new, f"{rel}:{i + 1} docstring"
        raise RuntimeError("no docstring")

    def syntax_edit(self):
        rel = self.pick_py()
        src = self.read(rel)
        lines = src.split("\n")
        for _ in range(100):
            i = self.rng.randrange(len(lines))
            if lines[i].strip() and not lines[i].strip().startswith("#"):
                new_lines = list(lines)
                new_lines[i] = lines[i] + self.rng.choice([" )", ":", " +"])
                new = "\n".join(new_lines)
                try:
                    ast.parse(new)
                except SyntaxError:
                    return rel, src, new, f"{rel}:{i + 1} syntax"
        raise RuntimeError("no syntax edit")


def make(gen: Gen, kind: str):
    rng = gen.rng
    if kind == "code":
        return [gen.code_edit()]
    if kind == "classlevel":
        return [gen.classlevel_edit()]
    if kind == "comment":
        return [gen.comment_edit()]
    if kind == "fixture":
        return [gen.fixture_edit()]
    if kind == "test":
        return [gen.test_edit() if rng.random() < 0.8 else gen.comment_edit(rng.choice(gen.tests))]
    if kind == "docstring":
        return [gen.docstring_edit()]
    if kind == "syntax":
        return [gen.syntax_edit()]
    if kind == "multi":
        parts = []
        used = set()
        for sub in rng.sample(["code", "comment", "fixture", "comment", "code"], rng.choice([2, 3])):
            for _ in range(10):
                edit = make(gen, sub)[0]
                if edit[0] not in used:
                    used.add(edit[0])
                    parts.append(edit)
                    break
        return parts
    raise ValueError(kind)


def special(gen: Gen, kind: str):
    rng = gen.rng
    if kind == "newfile":
        choice = rng.random()
        if choice < 0.4:
            rel = rng.choice(["sqlglot/dialects/newdb.py", "sqlglot/optimizer/extra_rule.py", "sqlglot/utils_extra.py"])
            text = "from sqlglot import exp\n\n\ndef helper(e):\n    return e\n"
        elif choice < 0.7:
            rel = "tests/fixtures/optimizer/new_fixture.sql"
            text = "SELECT 1;\nSELECT 1;\n"
        else:
            rel = "tests/test_new_feature.py"
            text = "import unittest\n\n\nclass TestNew(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(True)\n"
        return git_diff(rel, "", text, new_file=True), f"new file {rel}"
    if kind == "delete":
        rel = rng.choice(gen.fixtures + ["sqlglot/dialects/dune.py", "sqlglot/generators/tableau.py", "tests/dialects/test_solr.py"])
        old = gen.read(rel)
        return git_diff(rel, old, "", deleted=True), f"delete {rel}"
    raise ValueError(kind)


def main():
    repo, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    rng = random.Random(seed)
    gen = Gen(repo, rng)
    gen.uniform = "--uniform" in sys.argv or "--official" in sys.argv
    gen.official = "--official" in sys.argv
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    if gen.official:
        # the kit sample's mix: 16 code, 8 fixture, 3 multi, 3 comment in 30
        kinds = ["code"] * 16 + ["fixture"] * 8 + ["multi"] * 3 + ["comment"] * 3
    else:
        kinds = None
    kinds = kinds or (["classlevel"] * 100) if "--classlevel" in sys.argv else kinds or (["code"] * 46 + ["fixture"] * 18 + ["comment"] * 10 + ["multi"] * 10 + ["test"] * 6
             + ["docstring"] * 4 + ["syntax"] * 2 + ["newfile"] * 2 + ["delete"] * 2)
    meta = {}
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    for n in range(count):
        kind = rng.choice(kinds)
        cid = "".join(rng.choice(alphabet) for _ in range(4))
        while cid in meta:
            cid = "".join(rng.choice(alphabet) for _ in range(4))
        if kind in ("newfile", "delete"):
            patch, desc = special(gen, kind)
            descs = [desc]
        else:
            edits = make(gen, kind)
            patch = "".join(git_diff(rel, old, new) for rel, old, new, _ in edits)
            descs = [d for *_, d in edits]
        with open(os.path.join(out, "commits", cid + ".patch"), "w") as handle:
            handle.write(patch)
        meta[cid] = {"kind": kind, "desc": descs}
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
    print(f"wrote {count} patches to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
