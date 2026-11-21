"""Weak-mutation data for sqlglot's comparisons, from one instrumented run of the suite at
the pinned commit (tools/cmp_plugin.py). For each single comparison, as test masks in the
map's test order:
  ran      tests that executed it
  differs  tests where the neighbouring operator (is -> ==, is not -> !=, < -> <=,
           <= -> <, > -> >=, >= -> >) would have given a different result or raised
  outside  the comparison also ran outside any test (import, collection): no narrowing
Tests that failed in the instrumented run are listed so the selector keeps them.

usage: python build_cmp.py <sqlglot checkout> <blast_map.pkl.gz> <out.pkl.gz>
"""
import glob
import gzip
import json
import os
import pickle
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
    by_file.setdefault(parts[0], 0)
    by_file[parts[0]] |= bit[t]
    if len(parts) >= 3:
        key = "::".join(parts[:2])
        by_class[key] = by_class.get(key, 0) | bit[t]


def mask_of(ctx):
    if ctx.startswith("CLASS:"):
        return by_class.get(ctx[6:], 0)
    if ctx.startswith("FILE:"):
        return by_file.get(ctx[5:], 0)
    if ctx in bit:
        return bit[ctx]
    base = ctx.split("[")[0]
    if base in bit:
        return bit[base]
    m = 0
    for t, b in bit.items():
        if t.startswith(base):
            m |= b
    return m


out_base = "/tmp/cmp_run.json"
for f in glob.glob(out_base + "*"):
    os.remove(f)
env = dict(os.environ, CMP_OUT=out_base, PYTHONPATH=f"{here}{os.pathsep}{root}", SKIP_INTEGRATION="0",
           PYTHONDONTWRITEBYTECODE="1")
proc = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-n", "2",
                       "-p", "cmp_plugin", "--timeout", "1200"], cwd=root, env=env, capture_output=True, text=True)
print("instrumented run:", (proc.stdout.strip().splitlines() or ["?"])[-1])

ran, differs, outside, ops = {}, {}, set(), {}
failed = 0
files = glob.glob(out_base + ".*") or glob.glob(out_base)
for f in files:
    d = json.load(open(f))
    sites = [tuple(s) for s in d["sites"]]
    for idx, ctxs in d["ran"].items():
        rel, line, col, op = sites[int(idx)]
        key = (rel, line, col)
        ops[key] = op
        for c in ctxs:
            if c == "<outside>":
                outside.add(key)
            else:
                ran[key] = ran.get(key, 0) | mask_of(c)
    for idx, ctxs in d["differs"].items():
        rel, line, col, op = sites[int(idx)]
        key = (rel, line, col)
        for c in ctxs:
            if c == "<outside>":
                outside.add(key)
            else:
                differs[key] = differs.get(key, 0) | mask_of(c)
    for t in d["failed"]:
        failed |= mask_of(t)
with gzip.open(out_path, "wb") as fh:
    pickle.dump({"ran": ran, "differs": differs, "outside": outside, "ops": ops, "failed": failed}, fh)
narrowable = sum(1 for k in ran if k not in outside)
same_everywhere = sum(1 for k in ran if k not in outside and not differs.get(k))
print(f"{len(ran)} comparisons ran in tests, {narrowable} never outside tests, "
      f"{same_everywhere} whose neighbouring operator never differs; failed tests: {bin(failed).count('1')}")
