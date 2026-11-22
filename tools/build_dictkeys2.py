"""Per-dict key reads of sqlglot's class-level dict constants, added to dictkeys.pkl.gz.

dictkeys.pkl.gz (build_dictkeys.py, build_key_level.py, build_keywords_level.py, packed by
build_lazy.py) says, per attribute name, which tests looked up each key in any class's dict
of that name. This runs the suite once more with dictkeys2_plugin, which also files each
lookup under the class whose dict it was, and records which class-body dicts were built
from which. The selector then counts, for an edit to one class's entry, only the lookups in
that class's dict and in the dicts built from it (Selector.dict_owners).

The safety rules stay per name (unsafe names, key_level keys, full reads): this only adds
"keys_owned" (per name, pickled {owner: {key: test mask}}) and "derived". Lookups of a key the
per-name data has but this run filed under no class (a run-to-run difference) are added to
every class's entry for that key, so the per-class masks never lose a test the per-name
masks have.

usage: python build_dictkeys2.py <sqlglot checkout> <blast_map.pkl.gz> <tests.json> <dictkeys in> <dictkeys out> [workers]
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
map_path, tests_path, dk_in, dk_out = sys.argv[2:6]
workers = sys.argv[6] if len(sys.argv) > 6 else "2"
here = Path(__file__).resolve().parent

with gzip.open(map_path, "rb") as fh:
    doc = pickle.load(fh)
bit = {t: 1 << i for i, t in enumerate(doc["tests"])}
tests = json.load(open(tests_path))["tests"]
ids = [t["id"] for t in tests]
idset = set(ids)
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
    return [base] if base in idset else [c for c in ids if c.startswith(base)]


def mask(ctxs):
    m = 0
    for c in ctxs:
        if c == "<no test>":
            continue
        for t in expand(c):
            m |= bit.get(t, 0)
    return m


out_base = "/tmp/dictkeys2_run.json"
for f in glob.glob(out_base + "*"):
    os.remove(f)
env = dict(os.environ, DICTKEYS_OUT=out_base, PYTHONPATH=f"{here}{os.pathsep}{root}", SKIP_INTEGRATION="0",
           PYTHONDONTWRITEBYTECODE="1")
proc = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-n", workers,
                       "-p", "dictkeys2_plugin", "--timeout", "600"], cwd=root, env=env, capture_output=True, text=True)
tail = proc.stdout.strip().splitlines()[-1:] or ["?"]
print("instrumented run:", tail[0])
if proc.returncode != 0:
    print(proc.stdout[-3000:])
    sys.exit("the instrumented run must pass at the pinned commit")

owned, derived, per_name = {}, {}, {}
for f in glob.glob(out_base + ".*") or glob.glob(out_base):
    d = json.load(open(f))
    for attr, owners in d["owned"].items():
        for o, per in owners.items():
            for k, ctxs in per.items():
                m = mask(ctxs)
                dst = owned.setdefault(attr, {}).setdefault(o, {})
                dst[k] = dst.get(k, 0) | m
                per_name.setdefault(attr, {})
                per_name[attr][k] = per_name[attr].get(k, 0) | m
    for attr, owners in d["derived"].items():
        dst = derived.setdefault(attr, {})
        for o, srcs in owners.items():
            if o in dst and (dst[o] is None or srcs is None):
                dst[o] = None
            elif o in dst:
                dst[o] = sorted(set(dst[o]) | set(srcs))
            else:
                dst[o] = srcs

with gzip.open(dk_in, "rb") as fh:
    data = pickle.load(fh)
assert data.get("format") == 3 and data["tests"] == doc["tests"]
added = 0
packed = {}
for attr in data["tracked"]:
    if attr not in owned:
        continue
    old = pickle.loads(data["keys"][attr]) if attr in data["keys"] else {}
    mine = owned[attr]
    for k, m in old.items():
        extra = m & ~per_name.get(attr, {}).get(k, 0)
        if extra:
            added += bin(extra).count("1")
            for o in mine:
                mine[o][k] = mine[o].get(k, 0) | extra
            if not mine:
                mine["?"] = {k: extra}
    packed[attr] = pickle.dumps(mine, protocol=4)
data["keys_owned"] = packed
data["derived"] = {a: v for a, v in derived.items() if a in packed}
with gzip.open(dk_out, "wb", compresslevel=9) as fh:
    pickle.dump(data, fh, protocol=4)
unknown = sum(1 for v in data["derived"].values() for s in v.values() if s is None)
total = sum(len(v) for v in data["derived"].values())
print(f"{len(packed)} names with per-class reads; {total} class dicts, {unknown} with unknown sources; "
      f"{added} test/key pairs only the per-name data had")
for a in ("FUNCTIONS", "KEYWORDS", "FUNCTION_PARSERS"):
    v = data["derived"].get(a, {})
    print(a, len(v), "dicts,", sum(1 for s in v.values() if s is None), "unknown:",
          sorted(o for o, s in v.items() if s is None)[:20])
