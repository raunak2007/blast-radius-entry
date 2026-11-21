"""Per-key reads of sqlglot's class-level dict constants, at the pinned commit.

Runs the test suite once with dictkeys_plugin, then decides which dict attributes are safe
to narrow by key. An attribute name is unsafe if, for any class that has it:
  - its dict was still referenced elsewhere after the swap (an alias made at import time),
  - it was written to at runtime,
  - sqlglot code read it whole at runtime (it may be copied into a cache that later tests
    read instead, like the generator's dispatch table),
  - something read it outside any test or test file,
  - code that runs at import time reads it, other than a class body building the same
    attribute from a parent (`NAME = {**Parent.NAME, ...}`): a structure derived from it
    under another name would carry an edit to tests that never read the dict itself.
Full reads from test code just keep those tests.

usage: python build_dictkeys.py <sqlglot checkout> <unused> <tests.json> <out.pkl.gz>
"""
import ast
import glob
import gzip
import json
import os
import pickle
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
map_path, tests_path, out_path = sys.argv[2:5]
here = Path(__file__).resolve().parent

tests = json.load(open(tests_path))["tests"]
ids = [t["id"] for t in tests]
by_class, by_file = {}, {}
for tid in ids:
    parts = tid.split("::")
    by_file.setdefault(parts[0], []).append(tid)
    if len(parts) >= 3:
        by_class.setdefault("::".join(parts[:2]), []).append(tid)


def expand(ctx):
    if ctx.startswith("CLASS:"):
        return by_class.get(ctx[6:], [])
    if ctx.startswith("FILE:"):
        return by_file.get(ctx[5:], [])
    base = ctx.split("[")[0]
    return [base] if base in set(ids) else [c for c in ids if c.startswith(base)]


out_base = "/tmp/dictkeys_run.json"
for f in glob.glob(out_base + "*"):
    os.remove(f)
env = dict(os.environ, DICTKEYS_OUT=out_base, PYTHONPATH=f"{here}{os.pathsep}{root}", SKIP_INTEGRATION="0",
           PYTHONDONTWRITEBYTECODE="1")
proc = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-n", "2",
                       "-p", "dictkeys_plugin", "--timeout", "600"], cwd=root, env=env, capture_output=True, text=True)
tail = proc.stdout.strip().splitlines()[-1:] or ["?"]
print("instrumented run:", tail[0])
if proc.returncode != 0:
    print(proc.stdout[-3000:])
    sys.exit("the instrumented run must pass at the pinned commit")

keys, full_tests, unsafe, owners = {}, {}, {}, {}
for f in glob.glob(out_base + ".*") or glob.glob(out_base):
    d = json.load(open(f))
    for attr, per in d["keys"].items():
        for k, ctxs in per.items():
            for c in ctxs:
                if c == "<no test>":
                    unsafe.setdefault(attr, set()).add("read outside tests")
                    continue
                keys.setdefault(attr, {}).setdefault(k, set()).update(expand(c))
    for attr, per in d["full"].items():
        for origin, ctxs in per.items():
            if origin == "sqlglot":
                unsafe.setdefault(attr, set()).add("read whole by sqlglot at runtime")
            for c in ctxs:
                if c == "<no test>":
                    unsafe.setdefault(attr, set()).add("read whole outside tests")
                    continue
                full_tests.setdefault(attr, set()).update(expand(c))
    for attr in d["writes"]:
        unsafe.setdefault(attr, set()).add("written at runtime")
    for attr in d["aliased"]:
        unsafe.setdefault(attr, set()).add("aliased at import")
    for attr, own in d["owners"].items():
        owners.setdefault(attr, set()).update(own)

# import-time reads: from the sources, plus the lines that run while sqlglot itself is
# imported (traced in a fresh interpreter; reads made while test modules are collected are
# already filed under their test file by the plugin)
TRACE = r"""
import json, os, sys
root = sys.argv[1]
sys.path.insert(0, root)
seen = set()
prefix = os.path.join(root, "sqlglot") + os.sep
def tracer(frame, event, arg):
    fn = frame.f_code.co_filename
    if not fn.startswith(prefix):
        return None
    def local(frame, event, arg):
        if event == "line":
            seen.add((fn, frame.f_lineno))
        return local
    seen.add((fn, frame.f_lineno))
    return local
sys.settrace(tracer)
import importlib, pkgutil
import sqlglot
for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name == "sqlglot.__main__":
        continue
    try:
        importlib.import_module(info.name)
    except Exception:
        pass
sys.settrace(None)
print(json.dumps(sorted([os.path.relpath(f, root).replace(os.sep, "/"), n] for f, n in seen)))
"""
traced = json.loads(subprocess.run([sys.executable, "-I", "-c", TRACE, str(root)], capture_output=True, text=True,
                                   check=True).stdout)
import_lines = {}
for rel, n in traced:
    import_lines.setdefault(rel, set()).add(n)
print("lines run while importing sqlglot:", sum(len(v) for v in import_lines.values()))
attrs = set(owners)


class ImportTimeReads(ast.NodeVisitor):
    def __init__(self, rel):
        self.rel = rel
        self.func_depth = 0
        self.target = None  # attribute being assigned in a class body, while visiting its value
        self.class_depth = 0

    def report(self, name, node):
        if name not in attrs:
            return
        runs_at_import = self.func_depth == 0 or node.lineno in import_lines.get(self.rel, ())
        if not runs_at_import:
            return
        if self.func_depth == 0 and self.class_depth and self.target == name:
            return  # NAME = {**Parent.NAME, ...} or a comprehension over it, in a class body
        unsafe.setdefault(name, set()).add(f"read at import time in {self.rel}:{node.lineno}")

    def visit_Attribute(self, node):
        if isinstance(node.ctx, ast.Load):
            self.report(node.attr, node)
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and self.class_depth and self.func_depth == 0:
            self.report(node.id, node)

    def visit_FunctionDef(self, node):
        for d in node.decorator_list + node.args.defaults + [x for x in node.args.kw_defaults if x]:
            self.visit(d)
        self.func_depth += 1
        for s in node.body:
            self.visit(s)
        self.func_depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.func_depth += 1
        self.visit(node.body)
        self.func_depth -= 1

    def visit_ClassDef(self, node):
        for b in node.bases + node.decorator_list + [k.value for k in node.keywords]:
            self.visit(b)
        self.class_depth += 1
        for s in node.body:
            self.visit(s)
        self.class_depth -= 1

    def _assign(self, targets, value):
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        saved = self.target
        self.target = names[0] if len(names) == 1 and len(targets) == 1 and self.class_depth and self.func_depth == 0 else None
        if value is not None:
            self.visit(value)
        self.target = saved

    def visit_Assign(self, node):
        self._assign(node.targets, node.value)

    def visit_AnnAssign(self, node):
        self._assign([node.target], node.value)


for p in sorted((root / "sqlglot").rglob("*.py")):
    rel = p.relative_to(root).as_posix()
    ImportTimeReads(rel).visit(ast.parse(p.read_text(encoding="utf-8")))

data = {
    "keys": {a: {k: sorted(v) for k, v in per.items()} for a, per in keys.items()},
    "full_tests": {a: sorted(v) for a, v in full_tests.items()},
    "tracked": sorted(attrs),
    "unsafe": {a: sorted(v)[:5] for a, v in unsafe.items()},
}
with gzip.open(out_path, "wb") as fh:
    pickle.dump(data, fh)
safe = sorted(attrs - set(unsafe))
print(f"{len(attrs)} dict attributes tracked, {len(safe)} safe to narrow by key")
print("safe:", ", ".join(safe[:60]))
