"""Generate patches of further kinds the earlier sets lack (dev tool).

Deleted and renamed functions and methods (alone, or renamed together with every
reference in sqlglot), changed default values, added and removed parameters,
added and removed decorators, edits of package re-exports (`__init__.py`), added
tests (passing and failing), whitespace-only edits, and multi-file mixes of these.
Labels come from tools/label_pytest.py.

Usage: python tools/gen_kinds2.py <sqlglot checkout> <out dir> <count> [seed]
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


class Skip(RuntimeError):
    pass


class Kinds2:
    def __init__(self, repo, rng):
        self.repo = repo
        self.rng = rng
        self.py = sorted(os.path.relpath(os.path.join(d, f), repo) for d, dn, fs in os.walk(os.path.join(repo, "sqlglot"))
                         for f in fs if f.endswith(".py") and "__pycache__" not in d)
        self.tests = sorted(os.path.relpath(os.path.join(d, f), repo) for d, dn, fs in os.walk(os.path.join(repo, "tests"))
                            for f in fs if f.startswith("test_") and f.endswith(".py") and "fixtures" not in d)
        self._trees = {}

    def read(self, rel):
        return open(os.path.join(self.repo, rel), encoding="utf-8").read()

    def tree(self, rel):
        if rel not in self._trees:
            self._trees[rel] = ast.parse(self.read(rel))
        return self._trees[rel]

    def pick(self, items):
        if not items:
            raise Skip("nothing to pick")
        return self.rng.choice(items)

    def defs(self, rel, where=("module", "class")):
        """(def node, enclosing class or None, enclosing body) for defs that are direct
        children of the module or of a top-level class."""
        out = []
        tree = self.tree(rel)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and "module" in where:
                out.append((node, None, tree.body))
            if isinstance(node, ast.ClassDef) and "class" in where:
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out.append((item, node, node.body))
        return out

    def _replace_lines(self, rel, first, last, new_lines):
        lines = self.read(rel).splitlines(keepends=True)
        new = lines[: first - 1] + new_lines + lines[last:]
        return "".join(new)

    def _replace_segment(self, rel, node, text):
        if node.lineno != node.end_lineno:
            raise Skip("multi-line segment")
        lines = self.read(rel).splitlines(keepends=True)
        line = lines[node.lineno - 1]
        b = line.encode("utf-8")
        nb = b[: node.col_offset] + text.encode("utf-8") + b[node.end_col_offset:]
        lines[node.lineno - 1] = nb.decode("utf-8")
        return "".join(lines)

    def _code_file(self):
        return self.pick([p for p in self.py if not p.endswith("__init__.py") and "/executor/" not in p
                          or p.endswith("expressions/__init__.py")])

    # ------------------------------------------------------------------
    def func_delete(self):
        rel = self._code_file()
        cands = [(n, c, body) for n, c, body in self.defs(rel) if len(body) > 1 and not n.name.startswith("__")]
        node, cls, _ = self.pick(cands)
        first = min([d.lineno for d in node.decorator_list] + [node.lineno])
        new = self._replace_lines(rel, first, node.end_lineno, [])
        where = f"{cls.name}." if cls else ""
        return git_diff(rel, self.read(rel), new), f"{rel}: delete {where}{node.name}"

    def func_rename(self):
        rel = self._code_file()
        node, cls, _ = self.pick([(n, c, b) for n, c, b in self.defs(rel) if not n.name.startswith("__")])
        lines = self.read(rel).splitlines(keepends=True)
        line = lines[node.lineno - 1]
        new_name = node.name + self.pick(["_v2", "_old", "_impl"])
        if f"def {node.name}(" not in line:
            raise Skip("def line")
        lines[node.lineno - 1] = line.replace(f"def {node.name}(", f"def {new_name}(", 1)
        where = f"{cls.name}." if cls else ""
        return git_diff(rel, self.read(rel), "".join(lines)), f"{rel}: rename {where}{node.name} -> {new_name} (callers unchanged)"

    def func_rename_everywhere(self):
        rel = self._code_file()
        node, cls, _ = self.pick([(n, c, b) for n, c, b in self.defs(rel)
                                  if not n.name.startswith("__") and len(n.name) >= 6])
        name = node.name
        new_name = name + "_renamed"
        word = re.compile(rf"\b{re.escape(name)}\b")
        parts, files = [], 0
        for path in self.py:
            text = self.read(path)
            if not word.search(text):
                continue
            new = word.sub(new_name, text)
            parts.append(git_diff(path, text, new))
            files += 1
        if files > 12:
            raise Skip("too many files")
        where = f"{cls.name}." if cls else ""
        return "".join(parts), f"{rel}: rename {where}{name} -> {new_name} and every reference in sqlglot/ ({files} files)"

    def default_change(self):
        rel = self._code_file()
        cands = []
        for node, cls, _ in self.defs(rel):
            for d in list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]:
                if isinstance(d, ast.Constant) and d.lineno == d.end_lineno:
                    cands.append((node, cls, d))
        node, cls, d = self.pick(cands)
        v = d.value
        if isinstance(v, bool):
            text = repr(not v)
        elif v is None:
            text = self.pick(["0", "''", "False"])
        elif isinstance(v, int):
            text = repr(v + self.pick([1, -1]))
        elif isinstance(v, float):
            text = repr(v * 2 + 1)
        elif isinstance(v, str):
            text = repr(v + "x") if v else repr("x")
        else:
            raise Skip("default kind")
        new = self._replace_segment(rel, d, text)
        where = f"{cls.name}." if cls else ""
        return git_diff(rel, self.read(rel), new), f"{rel}: {where}{node.name} default {ast.unparse(d)} -> {text}"

    def param_add(self):
        rel = self._code_file()
        cands = [(n, c) for n, c, _ in self.defs(rel)
                 if n.args.args and not n.args.vararg and not n.args.kwarg and not n.args.kwonlyargs
                 and n.args.args[-1].end_lineno == n.lineno]
        node, cls = self.pick(cands)
        last = node.args.defaults[-1] if node.args.defaults else node.args.args[-1]
        if last.end_lineno != node.lineno:
            raise Skip("multi-line header")
        required = not node.args.defaults and self.rng.random() < 0.5
        extra = ", extra_arg" if required else ", extra_arg=None"
        lines = self.read(rel).splitlines(keepends=True)
        b = lines[node.lineno - 1].encode("utf-8")
        lines[node.lineno - 1] = (b[: last.end_col_offset] + extra.encode() + b[last.end_col_offset:]).decode("utf-8")
        where = f"{cls.name}." if cls else ""
        return git_diff(rel, self.read(rel), "".join(lines)), f"{rel}: {where}{node.name} gains parameter{extra}"

    def param_remove(self):
        rel = self._code_file()
        cands = []
        for node, cls, _ in self.defs(rel):
            a = node.args
            if len(a.args) >= 2 and a.defaults and not a.vararg and not a.kwonlyargs and not a.kwarg:
                prev_end = a.defaults[-2] if len(a.defaults) >= 2 else a.args[-2]
                if prev_end.end_lineno == a.defaults[-1].end_lineno == node.lineno:
                    cands.append((node, cls, prev_end, a.args[-1], a.defaults[-1]))
        node, cls, prev_end, arg, default = self.pick(cands)
        lines = self.read(rel).splitlines(keepends=True)
        b = lines[node.lineno - 1].encode("utf-8")
        lines[node.lineno - 1] = (b[: prev_end.end_col_offset] + b[default.end_col_offset:]).decode("utf-8")
        where = f"{cls.name}." if cls else ""
        return git_diff(rel, self.read(rel), "".join(lines)), f"{rel}: {where}{node.name} loses parameter {arg.arg}"

    def decorator_add(self):
        rel = self._code_file()
        cands = [(n, c) for n, c, _ in self.defs(rel, ("class",))
                 if not n.decorator_list and n.args.args and n.args.args[0].arg == "self" and not n.name.startswith("__")]
        node, cls = self.pick(cands)
        deco = self.pick(["@staticmethod", "@classmethod", "@property"])
        indent = re.match(r"\s*", self.read(rel).splitlines()[node.lineno - 1]).group(0)
        new = self._replace_lines(rel, node.lineno, node.lineno - 1, [f"{indent}{deco}\n"])
        return git_diff(rel, self.read(rel), new), f"{rel}: {cls.name}.{node.name} gains {deco}"

    def decorator_remove(self):
        rel = self._code_file()
        cands = [(n, c, d) for n, c, _ in self.defs(rel) for d in n.decorator_list
                 if d.lineno == d.end_lineno and not ast.unparse(d).endswith("overload")]
        node, cls, d = self.pick(cands)
        new = self._replace_lines(rel, d.lineno, d.end_lineno, [])
        where = f"{cls.name}." if cls else ""
        return git_diff(rel, self.read(rel), new), f"{rel}: {where}{node.name} loses @{ast.unparse(d)}"

    def init_reexport(self):
        inits = [p for p in self.py if p.endswith("__init__.py")]
        rel = self.pick([p for p in inits if any(isinstance(n, ast.ImportFrom) and len(n.names) >= 2
                                                for n in self.tree(p).body)])
        node = self.pick([n for n in self.tree(rel).body if isinstance(n, ast.ImportFrom) and len(n.names) >= 2])
        dropped = self.pick(node.names)
        kept = [a for a in node.names if a is not dropped]
        new_node = ast.ImportFrom(module=node.module, names=kept, level=node.level)
        text = ast.unparse(new_node) + "\n"
        new = self._replace_lines(rel, node.lineno, node.end_lineno, [text])
        name = dropped.asname or dropped.name
        return git_diff(rel, self.read(rel), new), f"{rel}: no longer re-exports {name} (from {node.module})"

    def add_test(self):
        rel = self.pick(self.tests)
        classes = [n for n in self.tree(rel).body if isinstance(n, ast.ClassDef)]
        cls = self.pick(classes)
        kind = self.pick(["pass", "fail", "parse"])
        body = {
            "pass": "        self.assertEqual(1, 1)\n",
            "fail": "        self.assertEqual(1, 2)\n",
            "parse": "        from sqlglot import parse_one\n\n        self.assertEqual(parse_one('SELECT 1').sql(), 'SELECT 1')\n",
        }[kind]
        method = f"\n    def test_added_by_patch(self):\n{body}"
        new = self._replace_lines(rel, cls.end_lineno + 1, cls.end_lineno, [method])
        return git_diff(rel, self.read(rel), new), f"{rel}: {cls.name} gains test_added_by_patch ({kind})"

    def whitespace(self):
        rel = self.pick(self.py)
        lines = self.read(rel).splitlines(keepends=True)
        idx = [i for i, line in enumerate(lines) if line.strip() and not line.rstrip("\n").endswith("\\")]
        chosen = sorted(self.rng.sample(idx, min(3, len(idx))))
        for i in chosen:
            lines[i] = lines[i].rstrip("\n") + "   \n"
        return git_diff(rel, self.read(rel), "".join(lines)), f"{rel}: trailing whitespace on {len(chosen)} lines"


    # -- structural edits of classes and module globals (--more) ---------------
    def _classes(self, rel):
        return [n for n in self.tree(rel).body if isinstance(n, ast.ClassDef)]

    def class_rename(self):
        rel = self._code_file()
        cls = self.pick(self._classes(rel))
        lines = self.read(rel).splitlines(keepends=True)
        line = lines[cls.lineno - 1]
        if f"class {cls.name}" not in line:
            raise Skip("class line")
        lines[cls.lineno - 1] = line.replace(f"class {cls.name}", f"class {cls.name}Renamed", 1)
        return git_diff(rel, self.read(rel), "".join(lines)), f"{rel}: rename class {cls.name} (uses unchanged)"

    def class_base_change(self):
        rel = self._code_file()
        cands = [c for c in self._classes(rel) if c.bases and c.lineno == c.bases[-1].end_lineno]
        cls = self.pick(cands)
        base = cls.bases[0]
        new = self._replace_segment(rel, base, "object") if len(cls.bases) == 1 else None
        if new is None:
            lines = self.read(rel).splitlines(keepends=True)
            b = lines[cls.lineno - 1].encode("utf-8")
            first, second = cls.bases[0], cls.bases[1]
            lines[cls.lineno - 1] = (b[: first.col_offset] + b[second.col_offset:]).decode("utf-8")
            new = "".join(lines)
            desc = f"drops base {ast.unparse(first)}"
        else:
            desc = f"base {ast.unparse(base)} -> object"
        return git_diff(rel, self.read(rel), new), f"{rel}: class {cls.name} {desc}"

    def class_delete(self):
        rel = self._code_file()
        tree = self.tree(rel)
        cands = [c for c in self._classes(rel) if len(tree.body) > 1]
        cls = self.pick(cands)
        first = min([d.lineno for d in cls.decorator_list] + [cls.lineno])
        new = self._replace_lines(rel, first, cls.end_lineno, [])
        return git_diff(rel, self.read(rel), new), f"{rel}: delete class {cls.name}"

    def global_delete(self):
        rel = self._code_file()
        tree = self.tree(rel)
        cands = [n for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign)) and len(tree.body) > 1
                 and not any(isinstance(t, ast.Name) and t.id.startswith("__")
                             for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
        node = self.pick(cands)
        new = self._replace_lines(rel, node.lineno, node.end_lineno, [])
        return git_diff(rel, self.read(rel), new), f"{rel}: delete module-level `{ast.unparse(node)[:60]}`"

    def class_attr_delete(self):
        rel = self._code_file()
        cands = [(c, n) for c in self._classes(rel) for n in c.body
                 if isinstance(n, (ast.Assign, ast.AnnAssign)) and len(c.body) > 1]
        cls, node = self.pick(cands)
        new = self._replace_lines(rel, node.lineno, node.end_lineno, [])
        return git_diff(rel, self.read(rel), new), f"{rel}: delete {cls.name} attribute `{ast.unparse(node)[:60]}`"

    def raise_change(self):
        rel = self._code_file()
        cands = [n for n in ast.walk(self.tree(rel)) if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
                 and isinstance(n.exc.func, ast.Name) and n.exc.func.lineno == n.exc.func.end_lineno]
        node = self.pick(cands)
        new_name = "RuntimeError" if node.exc.func.id != "RuntimeError" else "ValueError"
        new = self._replace_segment(rel, node.exc.func, new_name)
        return git_diff(rel, self.read(rel), new), f"{rel}:{node.lineno} raise {node.exc.func.id} -> {new_name}"

    def property_to_method(self):
        rel = self._code_file()
        cands = [(n, c, d) for n, c, _ in self.defs(rel, ("class",)) for d in n.decorator_list
                 if ast.unparse(d) == "property"]
        node, cls, d = self.pick(cands)
        new = self._replace_lines(rel, d.lineno, d.end_lineno, [])
        return git_diff(rel, self.read(rel), new), f"{rel}: {cls.name}.{node.name} is no longer a property"


KINDS = ["func_delete", "func_rename", "func_rename_everywhere", "default_change", "param_add", "param_remove",
         "decorator_add", "decorator_remove", "init_reexport", "add_test", "whitespace", "multi"]
MORE = ["class_rename", "class_base_change", "class_delete", "global_delete", "class_attr_delete", "raise_change",
        "property_to_method", "init_reexport", "func_rename_everywhere", "multi"]


def main():
    repo, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    seed = int(sys.argv[4]) if len(sys.argv) > 4 and not sys.argv[4].startswith("--") else 11
    kinds = MORE if "--more" in sys.argv else KINDS
    rng = random.Random(seed)
    g = Kinds2(repo, rng)
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    meta = {}
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    n = attempts = 0
    while n < count and attempts < count * 50:
        attempts += 1
        kind = kinds[n % len(kinds)] if n < len(kinds) else rng.choice(kinds)
        try:
            if kind == "multi":
                parts, descs, used = [], [], set()
                pool = (["class_attr_delete", "raise_change", "global_delete", "add_test", "default_change"]
                        if kinds is MORE else
                        ["default_change", "param_add", "add_test", "decorator_remove", "whitespace", "func_delete"])
                for sub in rng.sample(pool, 2):
                    patch, desc = getattr(g, sub)()
                    paths = set(re.findall(r"diff --git a/(\S+)", patch))
                    if paths & used:
                        continue
                    used |= paths
                    parts.append(patch)
                    descs.append(desc)
                if len(parts) < 2:
                    continue
                patch = "".join(parts)
            else:
                patch, desc = getattr(g, kind)()
                descs = [desc]
        except (Skip, IndexError, ValueError):
            continue
        if not patch.strip():
            continue
        cid = "".join(rng.choice(alphabet) for _ in range(4))
        while cid in meta:
            cid = "".join(rng.choice(alphabet) for _ in range(4))
        with open(os.path.join(out, "commits", cid + ".patch"), "w") as handle:
            handle.write(patch)
        meta[cid] = {"kind": kind, "desc": descs}
        n += 1
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
    print(f"wrote {n} patches", file=sys.stderr)


if __name__ == "__main__":
    main()
