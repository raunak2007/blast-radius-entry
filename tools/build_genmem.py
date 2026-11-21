"""Data for checking test_generator_memory one dialect at a time, from the pinned commit.

test_generator_memory builds every dialect's generator 1,100 times and checks that the
last 1,000 builds leave less than 1 KB behind. A patch can only change that for a
dialect if it changes a module that is loaded when that dialect's generator is built.
For each dialect we start a fresh interpreter, import sqlglot, get the dialect, build a
generator, and record every sqlglot module loaded by then. We also record what importing
the test module itself loads, and time the test's per-dialect body.

usage: python build_genmem.py <sqlglot checkout> <out.pkl.gz>
"""
import ast
import gzip
import pickle
import sys
import time
from pathlib import Path

root = Path(sys.argv[1]).resolve()
out_path = sys.argv[2]
sys.path.insert(0, str(root))


def module_of(rel):
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


rel_of = {}
for p in sorted((root / "sqlglot").rglob("*.py")):
    rel = p.relative_to(root).as_posix()
    rel_of[module_of(rel)] = rel


def resolve(name):
    """The sqlglot source file for a dotted module name, or None."""
    return rel_of.get(name)


imports = {}
for mod, rel in rel_of.items():
    tree = ast.parse((root / rel).read_text(encoding="utf-8"))
    pkg = mod if rel.endswith("__init__.py") else mod.rpartition(".")[0]
    deps = set()
    for node in ast.walk(tree):  # everywhere: module level, functions, TYPE_CHECKING blocks
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                for i in range(1, len(parts) + 1):  # import a.b.c imports a, a.b, a.b.c
                    r = resolve(".".join(parts[:i]))
                    if r:
                        deps.add(r)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.split(".")
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            parts = target.split(".")
            for i in range(1, len(parts) + 1):
                r = resolve(".".join(parts[:i]))
                if r:
                    deps.add(r)
            for a in node.names:  # from pkg import submodule
                r = resolve(f"{target}.{a.name}")
                if r:
                    deps.add(r)
    # a package's __init__ runs before any of its submodules
    parts = mod.split(".")
    for i in range(1, len(parts)):
        r = resolve(".".join(parts[:i]))
        if r:
            deps.add(r)
    imports[rel] = deps


def closure(start):
    seen, todo = set(), list(start)
    while todo:
        r = todo.pop()
        if r in seen:
            continue
        seen.add(r)
        todo.extend(imports.get(r, ()))
    return seen


import gc  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
import tracemalloc  # noqa: E402

PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
import sqlglot
from sqlglot.dialects.dialect import Dialect, Dialects
name = Dialects(sys.argv[2])
d = Dialect.get_or_raise(name)
d.generator()
print(json.dumps(sorted(m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot."))))
"""

MODULE_PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
import tests.test_generator
print(json.dumps(sorted(m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot."))))
"""
# what importing the test module itself loads: an import-time failure there fails the test too
module_loaded = sorted({rel_of[m] for m in json.loads(subprocess.run(
    [sys.executable, "-I", "-c", MODULE_PROBE, str(root)], capture_output=True, text=True, check=True,
    cwd=str(root)).stdout) if m in rel_of})

from sqlglot.dialects.dialect import Dialect, Dialects  # noqa: E402

order, loaded, seconds = [], {}, {}
for name in Dialects:
    out = subprocess.run([sys.executable, "-I", "-c", PROBE, str(root), name.value],
                         capture_output=True, text=True, check=True).stdout
    mods = json.loads(out)
    order.append(name.value)
    loaded[name.value] = sorted({rel_of[m] for m in mods if m in rel_of})
    dialect = Dialect.get_or_raise(name)
    # the test's own per-dialect body, timed
    t0 = time.process_time()
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
    seconds[name.value] = time.process_time() - t0
    assert after - before < 1024, name

with gzip.open(out_path, "wb") as fh:
    pickle.dump({"order": order, "loaded": loaded, "seconds": seconds, "module_loaded": module_loaded}, fh)
sizes = sorted(len(v) for v in loaded.values())
print(f"{len(order)} dialects, {sizes[0]}..{sizes[-1]} of {len(rel_of)} modules loaded each, "
      f"{sum(seconds.values()):.2f}s for all bodies")
