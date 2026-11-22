"""Merge defaults_plugin outputs into defaults.pkl.gz, with test masks in blast_map's order.

usage: python build_defaults.py <defaults pickle prefix> <blast_map.pkl.gz> <out.pkl.gz>

Per function (rel, first line, name): its defaulted parameters, and per parameter the mask
of tests in which some call left it at its default (setup and teardown count for the
test's class). Contexts outside tests are kept apart ("outside"); the selector doesn't
narrow those parameters. Packed per source file. Built only from the pinned commit's run.
"""
import glob
import gzip
import pickle
import sys

prefix, map_path, out_path = sys.argv[1:4]
with gzip.open(map_path, "rb") as fh:
    doc = pickle.load(fh)
bit = {t: 1 << i for i, t in enumerate(doc["tests"])}


def ctx_mask(ctx):
    if ctx.startswith("CLASS:"):
        return doc["test_classes"].get(ctx[6:], 0)
    if ctx.startswith("FILE:"):
        return doc["test_files"].get(ctx[5:], 0)
    return bit.get(ctx, 0)


funcs, used, outside, unmatched = {}, {}, {}, set()
parts = sorted(glob.glob(prefix + "*"))
for path in parts:
    with open(path, "rb") as fh:
        part = pickle.load(fh)
    for key, names in part["funcs"].items():
        if funcs.setdefault(key, names) != names:
            funcs[key] = None  # the workers disagree on what this function takes: leave it out
    for key, per in part["records"].items():
        for ctx, names in per.items():
            if ctx == "IMPORT":
                outside.setdefault(key, set()).update(names)
                continue
            m = ctx_mask(ctx)
            if not m:
                unmatched.add(ctx)
            for name in names:
                d = used.setdefault(key, {})
                d[name] = d.get(name, 0) | m

by_file = {}
for key, names in funcs.items():
    if names is None:
        continue
    rel, first, name = key
    by_file.setdefault(rel, {})[(first, name)] = {
        "params": names, "used": used.get(key, {}), "outside": sorted(outside.get(key, ())),
    }
print(f"{len(parts)} parts; {sum(len(v) for v in by_file.values())} functions; "
      f"{sum(1 for v in by_file.values() for e in v.values() if e['outside'])} with defaults used outside tests; "
      f"{len(unmatched)} unmatched contexts", file=sys.stderr)
with gzip.open(out_path, "wb", compresslevel=9) as fh:
    pickle.dump({"format": 3, "tests": doc["tests"],
                 "by_file": {rel: pickle.dumps(v, protocol=4) for rel, v in by_file.items()}}, fh, protocol=4)
