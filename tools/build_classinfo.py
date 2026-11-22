"""Class facts of the pinned sqlglot, for narrowing class-level (import-time) changes.

Imports every sqlglot module and writes maps/classinfo.json.gz with, for every
class defined in sqlglot (key "module:qualname"):

  file, start, end   source span of the class statement
  methods            first lines of functions defined directly in its body
  bases              keys of its direct sqlglot bases
  tracked            True if a sqlglot-defined Python __init__ is in its MRO, so
                     the tracer's instantiation record can see its instances
  family             id of the dialect family it belongs to (a Dialect subclass
                     together with its tokenizer/parser/generator classes)

Usage: python tools/build_classinfo.py <sqlglot checkout> <out file>
"""

import ast
import gzip
import importlib
import json
import os
import pkgutil
import sys
import types


def _fragments(node):
    """Constant string pieces of an attribute-name expression, or None if fully dynamic."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fragments(node.left), _fragments(node.right)
        return (left or []) + (right or [])
    return []


def dynamic_access_sites(repo):
    """Lines that read attributes by computed name (getattr with a non-literal
    name, vars(), dir(), iterating __dict__). Each gets a regex of the names it
    could read, or None when it could read anything."""
    import re

    sites = []
    for base in ("sqlglot", "tests"):
        for dirpath, dirnames, filenames in os.walk(os.path.join(repo, base)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__" and d != "fixtures"]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                rel = os.path.relpath(path, repo)
                try:
                    tree = ast.parse(open(path, encoding="utf-8").read())
                except SyntaxError:
                    continue
                for func in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.Module, ast.ClassDef))]:
                    suffixes = set()
                    body_nodes = list(ast.walk(func))
                    for n in body_nodes:
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("endswith", "startswith") and n.args:
                            frag = n.args[0]
                            if isinstance(frag, ast.Constant) and isinstance(frag.value, str) and n.func.attr == "endswith":
                                suffixes.add(frag.value)
                    for n in body_nodes:
                        if not isinstance(n, ast.Call):
                            continue
                        name = n.func.id if isinstance(n.func, ast.Name) else (n.func.attr if isinstance(n.func, ast.Attribute) else None)
                        pattern = "?"
                        kind = "object"
                        if name in ("getattr", "hasattr", "setattr", "delattr") and len(n.args) >= 2:
                            arg = n.args[1]
                            if isinstance(arg, ast.Constant):
                                continue  # a literal name: found by plain text search
                            target = n.args[0]
                            if (isinstance(target, ast.Name) and "module" in target.id.lower()) or (
                                isinstance(target, ast.Call) and "import_module" in ast.dump(target.func)
                            ):
                                kind = "module"
                            frags = _fragments(arg)
                            if frags:
                                pattern = ".*".join(re.escape(f) for f in frags)
                                pattern = ".*" + pattern + ".*"
                            elif suffixes:
                                pattern = "|".join(".*" + re.escape(x) for x in sorted(suffixes))
                            else:
                                pattern = None
                        elif name == "vars" and isinstance(n.func, ast.Name):
                            pattern, kind = None, "instance"
                        elif name == "getmembers":
                            pattern = None
                            if n.args and "modules" in ast.dump(n.args[0]) or (n.args and isinstance(n.args[0], ast.Name) and "module" in n.args[0].id.lower()):
                                kind = "module"
                        else:
                            continue
                        sites.append([rel, n.lineno, pattern, kind])
                for n in ast.walk(tree):
                    if isinstance(n, ast.Attribute) and n.attr == "__dict__":
                        sites.append([rel, n.lineno, "__dict__", "object"])
    # __dict__ uses followed by a literal lookup are static; keep the rest as wildcards.
    out = []
    seen = set()
    for rel, line, pattern, kind in sites:
        if pattern == "__dict__":
            lines = open(os.path.join(repo, rel), encoding="utf-8").read().split("\n")
            text = " ".join(l.strip() for l in lines[line - 1 : line + 2])
            if re.search(r"__dict__\.get\(\s*[\"']|[\"'][A-Za-z_0-9]+[\"']\s+in\s+\w+(\.\w+)*\.__dict__|__dict__\[\s*[\"']", text):
                continue
            pattern = None
        key = (rel, line, pattern, kind)
        if key not in seen:
            seen.add(key)
            out.append([rel, line, pattern, kind])
    return out


def test_spans(repo):
    """{test file: [[first line, last line, node id], ...]} for every test method."""
    spans = {}
    for dirpath, dirnames, filenames in os.walk(os.path.join(repo, "tests")):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "fixtures")]
        for filename in filenames:
            if not (filename.startswith("test_") and filename.endswith(".py")):
                continue
            path = os.path.join(dirpath, filename)
            rel = os.path.relpath(path, repo)
            tree = ast.parse(open(path, encoding="utf-8").read())
            out = []
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test"):
                            start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                            out.append([start, child.end_lineno, f"{rel}::{node.name}::{child.name}"])
            spans[rel] = out
    return spans


def main():
    repo, out = sys.argv[1:3]
    out = os.path.abspath(out)
    repo = os.path.realpath(repo)
    sys.path.insert(0, repo)
    os.chdir(repo)
    import sqlglot

    modules = {"sqlglot": sqlglot}
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name == "sqlglot.__main__":
            continue
        try:
            modules[info.name] = importlib.import_module(info.name)
        except Exception as error:  # noqa: BLE001
            print("skip", info.name, error, file=sys.stderr)

    classes = {}

    def key_of(cls):
        return f"{cls.__module__}:{cls.__qualname__}"

    def visit(cls):
        k = key_of(cls)
        if k in classes or not str(cls.__module__).startswith("sqlglot"):
            return
        tracked = False
        for base in cls.__mro__:
            if base is object or not str(getattr(base, "__module__", "")).startswith("sqlglot"):
                continue
            if isinstance(base.__dict__.get("__init__"), types.FunctionType):
                tracked = True
                break
        classes[k] = {
            "bases": [key_of(b) for b in cls.__bases__ if str(b.__module__).startswith("sqlglot")],
            "tracked": tracked,
        }
        for value in list(cls.__dict__.values()):
            if isinstance(value, type):
                visit(value)

    for name, module in modules.items():
        for value in list(vars(module).values()):
            if isinstance(value, type) and value.__module__ == name:
                visit(value)

    # Dialect families.
    from sqlglot.dialects.dialect import Dialect

    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for k in classes:
        parent.setdefault(k, k)
    links = []
    users = {}
    for name, module in modules.items():
        for value in list(vars(module).values()):
            if isinstance(value, type) and issubclass(value, Dialect) and value.__module__ == name:
                dk = key_of(value)
                members = set()
                for attr in ("tokenizer_class", "jsonpath_tokenizer_class", "parser_class", "generator_class"):
                    member = getattr(value, attr, None)
                    if isinstance(member, type) and key_of(member) in classes:
                        members.add(key_of(member))
                for member in value.__dict__.values():
                    if isinstance(member, type) and key_of(member) in classes:
                        members.add(key_of(member))
                for m in members:
                    users.setdefault(m, set()).add(dk)
                    links.append((dk, m))
    # Only link classes owned by one dialect; shared bases would merge everything.
    for dk, m in links:
        if len(users[m]) == 1:
            union(dk, m)
    for k in classes:
        classes[k]["family"] = find(k)

    # Source spans and direct methods from the AST.
    for name, module in modules.items():
        path = getattr(module, "__file__", None)
        if not path or not path.endswith(".py"):
            continue
        rel = os.path.relpath(os.path.realpath(path), repo)
        tree = ast.parse(open(path, encoding="utf-8").read())

        def walk(body, prefix):
            for node in body:
                if isinstance(node, ast.ClassDef):
                    qual = f"{prefix}{node.name}"
                    k = f"{name}:{qual}"
                    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    methods = []
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            methods.append(min([child.lineno] + [d.lineno for d in child.decorator_list]))
                            methods.append(child.lineno)
                        elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                            for sub in ast.walk(child):
                                if isinstance(sub, ast.Lambda):
                                    methods.append(sub.lineno)
                    if k in classes:
                        classes[k].update({"file": rel, "start": start, "end": node.end_lineno, "methods": sorted(set(methods))})
                    walk(node.body, qual + ".")
                elif isinstance(node, (ast.If, ast.Try)):
                    walk(node.body, prefix)
                    walk(getattr(node, "orelse", []), prefix)

        walk(tree.body, "")
    classes["__dynamic__"] = dynamic_access_sites(repo)
    classes["__testspans__"] = test_spans(repo)
    with gzip.open(out, "wt", compresslevel=9) as handle:
        json.dump(classes, handle, separators=(",", ":"))
    real = {k: v for k, v in classes.items() if not k.startswith("__")}
    tracked = sum(1 for v in real.values() if v["tracked"])
    print(f"classes {len(real)} tracked {tracked} families {len(set(v['family'] for v in real.values()))} "
          f"dynamic sites {len(classes['__dynamic__'])}", file=sys.stderr)


if __name__ == "__main__":
    main()
