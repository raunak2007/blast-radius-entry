"""Generate patches of kinds the practice set does not have (dev tool).

renames/moves, new and deleted files, test-file and shared-helper edits,
configuration/packaging edits, docstrings, type annotations, dead code,
formatting-only rewrites, dict/set entries in dialect class bodies, sqlglotc,
and multi-file mixes of these. Labels come from tools/label_pytest.py.

Usage: python tools/gen_kinds.py <sqlglot checkout> <out dir> <count> [seed]
"""

from __future__ import annotations

import ast
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen_mutants import git_diff  # noqa: E402


def rename_diff(old, new, old_text, new_text=None):
    """git-style rename (optionally with content changes)."""
    if new_text is None or new_text == old_text:
        return f"diff --git a/{old} b/{new}\nsimilarity index 100%\nrename from {old}\nrename to {new}\n"
    body = git_diff(new, old_text, new_text).split("\n", 1)[1]
    body = body.replace(f"--- a/{new}", f"--- a/{old}")
    return f"diff --git a/{old} b/{new}\nsimilarity index 90%\nrename from {old}\nrename to {new}\n" + body


class Kinds:
    def __init__(self, repo, rng):
        self.repo = repo
        self.rng = rng
        self.py = sorted(os.path.relpath(os.path.join(d, f), repo) for d, dn, fs in os.walk(os.path.join(repo, "sqlglot"))
                         for f in fs if f.endswith(".py") and "__pycache__" not in d)
        self.tests = sorted(os.path.relpath(os.path.join(d, f), repo) for d, dn, fs in os.walk(os.path.join(repo, "tests"))
                            for f in fs if f.startswith("test_") and f.endswith(".py") and "fixtures" not in d)
        self.fixtures = sorted(os.path.relpath(os.path.join(d, f), repo) for d, dn, fs in os.walk(os.path.join(repo, "tests", "fixtures"))
                               for f in fs if f.endswith(".sql"))

    def read(self, rel):
        return open(os.path.join(self.repo, rel), encoding="utf-8").read()

    def pick(self, items):
        return self.rng.choice(items)

    # ------------------------------------------------------------------
    def rename_module(self):
        rel = self.pick([p for p in self.py if "/dialects/" in p or "/generators/" in p or "/parsers/" in p
                         or "/typing/" in p or "/optimizer/" in p])
        new = rel[:-3] + self.pick(["_v2", "_old", "_impl"]) + ".py"
        return rename_diff(rel, new, self.read(rel)), f"rename {rel} -> {new}"

    def move_test(self):
        rel = self.pick(self.tests)
        new = os.path.join(os.path.dirname(rel), "moved_" + os.path.basename(rel))
        return rename_diff(rel, new, self.read(rel)), f"rename {rel} -> {new}"

    def new_module(self):
        where = self.pick(["sqlglot/parsers", "sqlglot/generators", "sqlglot/typing", "sqlglot/dialects",
                           "sqlglot/optimizer", "sqlglot/expressions", "sqlglot", "sqlglot/executor"])
        name = self.pick(["helpers_extra", "foo", "compat", "newdb"])
        text = self.pick([
            "from __future__ import annotations\n\n\ndef helper(x):\n    return x\n",
            "raise ImportError('not ready')\n",
            "from sqlglot import exp\n\nVALUE = exp.Literal.number(1)\n",
        ])
        rel = f"{where}/{name}.py"
        return git_diff(rel, "", text, new_file=True), f"new file {rel}"

    def new_test_file(self):
        rel = f"tests/test_{self.pick(['extra', 'regression', 'new'])}.py"
        text = self.pick([
            "import unittest\n\n\nclass TestExtra(unittest.TestCase):\n    def test_x(self):\n        self.assertEqual(1, 1)\n",
            "import unittest\n\nfrom sqlglot import parse_one\n\n\nclass TestExtra(unittest.TestCase):\n    def test_parse(self):\n        self.assertIsNotNone(parse_one('SELECT 1'))\n",
        ])
        return git_diff(rel, "", text, new_file=True), f"new file {rel}"

    def new_fixture(self):
        where = self.pick(["tests/fixtures", "tests/fixtures/optimizer", "tests/fixtures/optimizer/tpc-h"])
        rel = f"{where}/{self.pick(['extra', 'new_cases'])}.sql"
        return git_diff(rel, "", "SELECT 1;\nSELECT 1 AS \"_col_0\";\n", new_file=True), f"new file {rel}"

    def delete_file(self):
        rel = self.pick(self.py + self.fixtures + self.tests + ["sqlglotc/setup.py", "sqlglotc/MANIFEST.in", "setup.cfg"])
        return git_diff(rel, self.read(rel), "", deleted=True), f"delete {rel}"

    def config_edit(self):
        choice = self.rng.random()
        if choice < 0.25:
            rel = "pyproject.toml"
            old = self.read(rel)
            new = old.replace("line-length", "line-length", 1)
            m = re.search(r"line-length\s*=\s*(\d+)", old)
            if m:
                new = old[: m.start(1)] + str(int(m.group(1)) + 1) + old[m.end(1):]
            else:
                new = old + "\n# note\n"
        elif choice < 0.45:
            rel = "setup.cfg"
            old = self.read(rel)
            new = old.replace("disallow_untyped_calls = False", "disallow_untyped_calls = True")
        elif choice < 0.65:
            rel = "sqlglotc/setup.py"
            old = self.read(rel)
            lines = old.split("\n")
            i = self.rng.randrange(len(lines))
            new = "\n".join(lines[:i] + ["# build note"] + lines[i:])
        elif choice < 0.8:
            rel = "sqlglotc/pyproject.toml"
            old = self.read(rel)
            new = old + "\n# trailing note\n"
        else:
            rel = self.pick(["README.md", "CHANGELOG.md", "Makefile", "CONTRIBUTING.md"])
            old = self.read(rel)
            lines = old.split("\n")
            i = self.rng.randrange(len(lines))
            new = "\n".join(lines[:i] + ["edited line"] + lines[i:])
        return git_diff(rel, old, new), f"config edit {rel}"

    def _func_nodes(self, rel):
        tree = ast.parse(self.read(rel))
        return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def docstring_or_annotation(self):
        for _ in range(100):
            rel = self.pick(self.py)
            src = self.read(rel)
            lines = src.split("\n")
            funcs = self._func_nodes(rel)
            if not funcs:
                continue
            f = self.pick(funcs)
            kind = self.rng.random()
            if kind < 0.35:
                indent = " " * (f.body[0].col_offset)
                if ast.get_docstring(f):
                    i = f.body[0].lineno - 1
                    lines[i] = lines[i].replace('"""', '"""Note: ', 1)
                else:
                    lines.insert(f.body[0].lineno - 1, f'{indent}"""Added docstring."""')
                desc = "docstring"
            elif kind < 0.7:
                # change/add a return annotation
                i = f.lineno - 1
                line = lines[i]
                if "->" in line:
                    lines[i] = re.sub(r"->\s*[^:]+:", "-> t.Any:", line, count=1)
                elif line.rstrip().endswith("):"):
                    lines[i] = line.rstrip()[:-1] + " -> t.Any:"
                else:
                    continue
                desc = "annotation"
            else:
                # dead code: an unreachable statement after the first return, or an unused local
                indent = " " * (f.body[0].col_offset)
                lines.insert(f.body[0].lineno - 1, f"{indent}_unused_local = None")
                desc = "dead code (unused local)"
            new = "\n".join(lines)
            try:
                ast.parse(new)
            except SyntaxError:
                continue
            if new == src:
                continue
            return git_diff(rel, src, new), f"{desc} {rel}:{f.lineno}"
        raise RuntimeError("no docstring/annotation edit")

    def formatting(self):
        for _ in range(100):
            rel = self.pick(self.py)
            src = self.read(rel)
            lines = src.split("\n")
            i = self.rng.randrange(len(lines))
            line = lines[i]
            if ", " not in line or line.strip().startswith("#") or len(line) > 90:
                continue
            new_line = line.replace(", ", ",  ", 1) if self.rng.random() < 0.5 else line + "  "
            new = "\n".join(lines[:i] + [new_line] + lines[i + 1:])
            try:
                if ast.dump(ast.parse(new)) != ast.dump(ast.parse(src)):
                    continue
            except SyntaxError:
                continue
            return git_diff(rel, src, new), f"formatting {rel}:{i + 1}"
        raise RuntimeError("no formatting edit")

    def dict_entry(self):
        for _ in range(300):
            rel = self.pick([p for p in self.py if "/dialects/" in p or "/parsers/" in p or "/generators/" in p
                             or "/tokens" in p or "/typing/" in p])
            src = self.read(rel)
            tree = ast.parse(src)
            dicts = []
            for n in ast.walk(tree):
                if isinstance(n, ast.ClassDef):
                    for c in n.body:
                        if isinstance(c, (ast.Assign, ast.AnnAssign)) and isinstance(getattr(c, "value", None), (ast.Dict, ast.Set)):
                            dicts.append(c.value)
            if not dicts:
                continue
            d = self.pick(dicts)
            elts = d.keys if isinstance(d, ast.Dict) else d.elts
            elts = [e for e in elts if e is not None and e.lineno == e.end_lineno]
            if not elts:
                continue
            e = self.pick(elts)
            lines = src.split("\n")
            i = e.lineno - 1
            line = lines[i]
            op = self.rng.random()
            if op < 0.4:
                new_line = None  # remove the entry line
            elif op < 0.7 and isinstance(e, ast.Constant) and isinstance(e.value, str):
                seg = line[e.col_offset:e.end_col_offset]
                new_line = line[:e.col_offset] + seg[:-1] + "_X" + seg[-1] + line[e.end_col_offset:]
            else:
                new_line = line + "  # entry note"
            new_lines = lines[:i] + ([new_line] if new_line is not None else []) + lines[i + 1:]
            new = "\n".join(new_lines)
            try:
                if ast.dump(ast.parse(new)) == ast.dump(tree) and new_line is not None and "note" not in new_line:
                    continue
            except SyntaxError:
                continue
            return git_diff(rel, src, new), f"dict/set entry {rel}:{i + 1} {'removed' if new_line is None else 'edited'}"
        raise RuntimeError("no dict entry")

    def test_edit(self):
        for _ in range(100):
            rel = self.pick(self.tests + ["tests/dialects/test_dialect.py", "tests/helpers.py"])
            src = self.read(rel)
            lines = src.split("\n")
            kind = self.rng.random()
            i = self.rng.randrange(len(lines))
            line = lines[i]
            if kind < 0.4:
                m = list(re.finditer(r'"([^"\\]{4,60})"', line))
                if not m:
                    continue
                mm = self.pick(m)
                new_line = line[: mm.start(1)] + mm.group(1).replace(" ", "  ", 1) + line[mm.end(1):]
                desc = "test literal"
            elif kind < 0.7:
                if not line.strip() or line.strip().startswith(("#", "@", "def ", "class ")):
                    continue
                indent = line[: len(line) - len(line.lstrip())]
                lines.insert(i, f"{indent}# reviewed")
                new_line = None
                desc = "test comment"
            else:
                m = re.match(r"(\s*)def (test_\w+)\(self\):", line)
                if not m:
                    continue
                new_line = f"{m.group(1)}def {m.group(2)}(self):\n{m.group(1)}    self.skipTest('flaky')"
                desc = "test skipped"
            new = "\n".join(lines if new_line is None else lines[:i] + [new_line] + lines[i + 1:])
            if new == src:
                continue
            try:
                ast.parse(new)
            except SyntaxError:
                continue
            return git_diff(rel, src, new), f"{desc} {rel}:{i + 1}"
        raise RuntimeError("no test edit")

    def fixture_noop(self):
        rel = self.pick(self.fixtures)
        src = self.read(rel)
        lines = src.split("\n")
        for _ in range(100):
            i = self.rng.randrange(len(lines))
            if lines[i].strip() and not lines[i].startswith(("--", "#")):
                kind = self.rng.random()
                if kind < 0.5:
                    new_line = lines[i] + " "
                    desc = "fixture trailing space"
                else:
                    new_line = lines[i].replace(" ", " /* c */ ", 1) if " " in lines[i] else lines[i]
                    desc = "fixture inline comment"
                if new_line == lines[i]:
                    continue
                new = "\n".join(lines[:i] + [new_line] + lines[i + 1:])
                return git_diff(rel, src, new), f"{desc} {rel}:{i + 1}"
        raise RuntimeError("no fixture edit")


KINDS = ["rename_module", "move_test", "new_module", "new_test_file", "new_fixture", "delete_file", "config_edit",
         "docstring_or_annotation", "formatting", "dict_entry", "dict_entry", "test_edit", "test_edit", "fixture_noop",
         "docstring_or_annotation", "multi"]


def main():
    repo, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 7
    rng = random.Random(seed)
    g = Kinds(repo, rng)
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    meta = {}
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    n = 0
    while n < count:
        kind = KINDS[n % len(KINDS)] if n < len(KINDS) else rng.choice(KINDS)
        try:
            if kind == "multi":
                parts, descs, used = [], [], set()
                for sub in rng.sample(["docstring_or_annotation", "dict_entry", "test_edit", "fixture_noop", "formatting",
                                       "config_edit", "new_module"], 2):
                    patch, desc = getattr(g, sub)()
                    path = re.search(r"diff --git a/(\S+)", patch).group(1)
                    if path in used:
                        continue
                    used.add(path)
                    parts.append(patch)
                    descs.append(desc)
                patch = "".join(parts)
            else:
                patch, desc = getattr(g, kind)()
                descs = [desc]
        except RuntimeError:
            continue
        cid = "".join(rng.choice(alphabet) for _ in range(4))
        while cid in meta:
            cid = "".join(rng.choice(alphabet) for _ in range(4))
        with open(os.path.join(out, "commits", cid + ".patch"), "w") as handle:
            handle.write(patch)
        meta[cid] = {"kind": kind, "desc": descs}
        n += 1
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
    print(f"wrote {count} patches", file=sys.stderr)


if __name__ == "__main__":
    main()
