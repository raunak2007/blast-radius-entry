"""Which function bodies run while some other module is being imported.

The blast map knows which lines run at import time, but not on whose behalf. A function
whose body runs while its own module is imported (a builder called in its own class body)
affects that module's structures; one whose body runs while another module is imported
(the trie builder called by every tokenizer class, the error formatter reached by a
dialect's import-time parse) affects that other module's structures, and through them
every test that uses it.

This imports every sqlglot module and every test module in a fresh process, with coverage
switched to the module whose code is executing (a stack, so nested imports get their own
context), and records, per (file, line) inside a def body, the modules other than its own
whose import ran it.

usage: python build_import_users.py <sqlglot repo> <out.json>
"""
import ast
import importlib
import importlib.abc
import json
import os
import pkgutil
import sys

repo, out_path = sys.argv[1:3]
repo = os.path.realpath(repo)
sys.path.insert(0, repo)
os.chdir(repo)
os.environ.setdefault("SKIP_INTEGRATION", "0")

import coverage  # noqa: E402

cov = coverage.Coverage(data_file=None, source=[os.path.join(repo, "sqlglot")])
stack = ["<none>"]


class Wrap(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        for finder in sys.meta_path:
            if finder is self:
                continue
            find = getattr(finder, "find_spec", None)
            if find is None:
                continue
            spec = find(fullname, path, target)
            if spec is not None:
                break
        else:
            return None
        loader = spec.loader
        if loader is None or not hasattr(loader, "exec_module"):
            return spec
        real_exec = loader.exec_module

        def exec_module(module, _real=real_exec, _name=fullname):
            stack.append(_name)
            cov.switch_context(_name)
            try:
                _real(module)
            finally:
                stack.pop()
                cov.switch_context(stack[-1])

        try:
            loader.exec_module = exec_module
        except (AttributeError, TypeError):
            pass
        return spec


sys.meta_path.insert(0, Wrap())
cov.start()
cov.switch_context("<none>")
import sqlglot  # noqa: E402

names = sorted(m.name for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."))
tests = []
for dirpath, _, files in os.walk(os.path.join(repo, "tests")):
    for f in files:
        if f.startswith("test_") and f.endswith(".py"):
            rel = os.path.relpath(os.path.join(dirpath, f), repo)
            tests.append(rel[:-3].replace(os.sep, "."))
failed = []
for name in names + sorted(tests):
    try:
        importlib.import_module(name)
    except BaseException as e:  # noqa: BLE001
        failed.append((name, repr(e)[:120]))
cov.stop()
data = cov.get_data()

module_file = {}
for name, mod in list(sys.modules.items()):
    f = getattr(mod, "__file__", None)
    if f and os.path.realpath(f).startswith(repo + os.sep):
        module_file[name] = os.path.relpath(os.path.realpath(f), repo)

bodies = {}


def body_lines(rel):
    if rel not in bodies:
        tree = ast.parse(open(os.path.join(repo, rel)).read())
        lines = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for s in node.body:
                    lines.update(range(s.lineno, (s.end_lineno or s.lineno) + 1))
        bodies[rel] = lines
    return bodies[rel]


users = {}
for f in data.measured_files():
    rel = os.path.relpath(os.path.realpath(f), repo)
    if not rel.startswith("sqlglot/"):
        continue
    body = body_lines(rel)
    for ctx in data.measured_contexts():
        if ctx == "<none>":
            continue
        if module_file.get(ctx) == rel:
            continue  # its own module's import
        data.set_query_context(ctx)
        for ln in data.lines(f) or []:
            if ln in body:
                users.setdefault(rel, {}).setdefault(str(ln), []).append(ctx)
json.dump({"users": users, "module_file": module_file, "failed_imports": failed}, open(out_path, "w"))
n = sum(len(v) for v in users.values())
print(f"{n} function-body lines run during other modules' imports, in {len(users)} files; "
      f"{len(failed)} imports failed")


# ---- who is affected: each importer, and every module whose own import imports it

import sqlglot.dialects as _dialects  # noqa: E402
import sqlglot.optimizer as _optimizer  # noqa: E402

# packages whose attributes import a submodule on first access (a module __getattr__)
LAZY = {
    "sqlglot.dialects": {k: f"sqlglot.dialects.{v}" for k, v in _dialects.MODULE_BY_ATTRIBUTE.items()},
    "sqlglot.optimizer": {k: v[0] for k, v in _optimizer._LAZY_ATTRS.items()},
}


def static_imports(rel, modname):
    """Modules a file imports while it is imported (module body and class bodies)."""
    tree = ast.parse(open(os.path.join(repo, rel)).read())
    pkg = modname if rel.endswith("__init__.py") else modname.rpartition(".")[0]
    out = set()

    def visit(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(node, ast.Import):
                for a in node.names:
                    parts = a.name.split(".")
                    out.update(".".join(parts[:k]) for k in range(1, len(parts) + 1))
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = pkg.split(".")
                    base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                    mod = ".".join(base + ([node.module] if node.module else []))
                else:
                    mod = node.module or ""
                parts = mod.split(".")
                out.update(".".join(parts[:k]) for k in range(1, len(parts) + 1))
                for a in node.names:
                    out.add(f"{mod}.{a.name}")  # a submodule, if it is one
                    if a.name in LAZY.get(mod, {}):
                        out.add(LAZY[mod][a.name])  # a lazily imported attribute
            for field in ("body", "orelse", "finalbody", "handlers"):
                sub = getattr(node, field, None)
                if isinstance(sub, list):
                    visit([x for x in sub if isinstance(x, ast.AST)])

    visit(tree.body)
    return out


importers_of = {}
for name, rel in module_file.items():
    if not rel.endswith(".py"):
        continue
    for dep in static_imports(rel, name):
        if dep in module_file and dep != name:
            importers_of.setdefault(dep, set()).add(name)


def ancestors(name):
    seen, todo = set(), [name]
    while todo:
        cur = todo.pop()
        for parent in importers_of.get(cur, ()):
            if parent not in seen:
                seen.add(parent)
                todo.append(parent)
    return seen


# tests that import a module themselves while they run: `from sqlglot.dialects import X` or
# `sqlglot.dialects.X` inside a test function (in a helper or at class level: the whole file)
lazy_tests = {}


def lazy_targets(node):
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.ImportFrom) and not sub.level and sub.module in LAZY:
            out.update(LAZY[sub.module][a.name] for a in sub.names if a.name in LAZY[sub.module])
        elif isinstance(sub, ast.ImportFrom) and not sub.level and sub.module:
            out.add(sub.module)
        elif isinstance(sub, ast.Import):
            out.update(a.name for a in sub.names)
        elif isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Attribute):
            base = ast.unparse(sub.value)
            if base in LAZY and sub.attr in LAZY[base]:
                out.add(LAZY[base][sub.attr])
    return {m for m in out if m in module_file}


for dirpath, _, fnames in os.walk(os.path.join(repo, "tests")):
    for fname in fnames:
        if not (fname.startswith("test_") and fname.endswith(".py")):
            continue
        rel = os.path.relpath(os.path.join(dirpath, fname), repo)
        tree = ast.parse(open(os.path.join(repo, rel)).read())
        for top in tree.body:
            if isinstance(top, ast.ClassDef):
                for item in top.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        found = set()
                        for stmt in item.body:
                            found |= lazy_targets(stmt)
                        who = f"{rel}::{top.name}::{item.name}" if item.name.startswith("test") else f"FILE:{rel}"
                        for m in found:
                            lazy_tests.setdefault(m, set()).add(who)
            elif isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for m in lazy_targets(top):
                    lazy_tests.setdefault(m, set()).add(f"FILE:{rel}")

everything = {"sqlglot"}  # imported by every test
cross = {}
for rel, lines in users.items():
    for ln, mods in lines.items():
        affected = set(mods)
        for m in mods:
            affected |= ancestors(m)
        files = sorted({module_file[m] for m in affected if m in module_file})
        tests = sorted({t for m in affected for t in lazy_tests.get(m, ())})
        cross.setdefault(rel, {})[int(ln)] = {"all": bool(affected & everything), "files": files, "tests": tests}
json.dump({"users": users, "module_file": module_file, "failed_imports": failed,
           "cross": {rel: {str(k): v for k, v in d.items()} for rel, d in cross.items()}},
          open(out_path, "w"))
n_all = sum(1 for d in cross.values() for v in d.values() if v["all"])
print(f"cross-module lines: {sum(len(d) for d in cross.values())}, of which affect every test: {n_all}")
