"""Per (expression class, arg): tests that checked an expression of that class while the arg
was missing, from one instrumented run at the pinned commit (tools/argreq_plugin.py).

Also, statically: which classes build their arg_types from another class's
(`arg_types = {**Concat.arg_types, ...}`), so a flip there reaches them too, and which
classes have their arg_types read some other way at import time (no narrowing for those).
Test code that reads library arg_types or required_args directly keeps its whole file.

usage: python build_argreq.py <sqlglot checkout> <blast_map.pkl.gz> <out.pkl.gz>
"""
import ast
import glob
import gzip
import json
import os
import pickle
import re
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
map_path, out_path = sys.argv[2], sys.argv[3]
here = Path(__file__).resolve().parent

doc = pickle.load(gzip.open(map_path))
order = doc["tests"]
bit = {t: 1 << i for i, t in enumerate(order)}
by_class, by_file = {}, {}
for t in order:
    parts = t.split("::")
    by_file[parts[0]] = by_file.get(parts[0], 0) | bit[t]
    if len(parts) >= 3:
        k = "::".join(parts[:2])
        by_class[k] = by_class.get(k, 0) | bit[t]


def mask_of(ctx):
    if ctx.startswith("CLASS:"):
        return by_class.get(ctx[6:], 0)
    if ctx.startswith("FILE:"):
        return by_file.get(ctx[5:], 0)
    base = ctx.split("[")[0]
    if base in bit:
        return bit[base]
    m = 0
    for t, b in bit.items():
        if t.startswith(base):
            m |= b
    return m


out_base = "/tmp/argreq_run.json"
for f in glob.glob(out_base + "*"):
    os.remove(f)
env = dict(os.environ, ARGREQ_OUT=out_base, PYTHONPATH=f"{here}{os.pathsep}{root}", SKIP_INTEGRATION="0",
           PYTHONDONTWRITEBYTECODE="1")
proc = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-n", "2",
                       "-p", "argreq_plugin", "--timeout", "900"], cwd=root, env=env, capture_output=True, text=True)
summary = (proc.stdout.strip().splitlines() or ["?"])[-1]
print("instrumented run:", summary)
if proc.returncode != 0:
    sys.exit("the instrumented run must pass at the pinned commit")

missing, outside = {}, set()
for f in glob.glob(out_base + ".*") or glob.glob(out_base):
    for key, ctxs in json.load(open(f))["missing"].items():
        owner, k = key.rsplit("|", 1)
        for c in ctxs:
            if c == "<outside>":
                outside.add((owner, k))
            else:
                missing[(owner, k)] = missing.get((owner, k), 0) | mask_of(c)

# static: arg_types built from another class's, and other import-time reads of X.arg_types
derived, unsafe = {}, set()
for p in sorted((root / "sqlglot").rglob("*.py")):
    rel = p.relative_to(root).as_posix()
    module = rel[:-3].replace("/", ".")
    tree = ast.parse(p.read_text(encoding="utf-8"))
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for st in cls.body:
            for node in ast.walk(st):
                if not (isinstance(node, ast.Attribute) and node.attr == "arg_types" and isinstance(node.ctx, ast.Load)):
                    continue
                src = node.value.attr if isinstance(node.value, ast.Attribute) else getattr(node.value, "id", None)
                is_unpack = (isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name)
                             and st.targets[0].id == "arg_types" and isinstance(st.value, ast.Dict)
                             and any(k is None and v is node for k, v in zip(st.value.keys, st.value.values)))
                if is_unpack and src:
                    derived.setdefault(src, set()).add(f"{module}.{cls.name}")
                elif src:
                    unsafe.add(src)

test_reads = 0
for p in sorted((root / "tests").rglob("*.py")):
    text = p.read_text(encoding="utf-8")
    if re.search(r"\.arg_types\s*(\[|\.get|\.items|\.values)|required_args", text):
        test_reads |= by_file.get(p.relative_to(root).as_posix(), 0)

with gzip.open(out_path, "wb") as fh:
    pickle.dump({"missing": missing, "outside": outside, "derived": {k: sorted(v) for k, v in derived.items()},
                 "unsafe_names": sorted(unsafe), "test_reads": test_reads}, fh)
print(f"{len(missing)} (class, arg) pairs checked while missing in tests, {len(outside)} outside tests; "
      f"derived from: {sorted(derived)}; unsafe: {sorted(unsafe)}; test files reading arg_types: {bin(test_reads).count('1')} tests")
