"""Merge attrs_plugin outputs into attr_owners.pkl.gz, with test masks in blast_map's order.

usage: python build_attr_owners.py <attrs pickle prefix> <blast_map.pkl.gz> <out.pkl.gz>

Per class constant (rel, class qualname, name) that was wrapped: the mask of tests whose
reads resolved to it (setup and teardown count for the test's class), and whether it was
read outside any test. Packed per source file. Built only from the pinned commit's run.
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


wrapped, reads, outside, unmatched = set(), {}, set(), set()
parts = sorted(glob.glob(prefix + "*"))
for path in parts:
    with open(path, "rb") as fh:
        part = pickle.load(fh)
    wrapped.update(map(tuple, part["wrapped"]))
    for key, ctxs in part["reads"].items():
        for ctx in ctxs:
            if ctx == "IMPORT":
                outside.add(key)
                continue
            m = ctx_mask(ctx)
            if not m:
                unmatched.add(ctx)
            reads[key] = reads.get(key, 0) | m
by_file = {}
for rel, qual, name in wrapped:
    by_file.setdefault(rel, {})[(qual, name)] = {"mask": reads.get((rel, qual, name), 0),
                                                 "outside": (rel, qual, name) in outside}
print(f"{len(parts)} parts; {len(wrapped)} constants, {len(reads)} read in tests, {len(outside)} read outside tests; "
      f"{len(unmatched)} unmatched contexts", file=sys.stderr)
with gzip.open(out_path, "wb", compresslevel=9) as fh:
    pickle.dump({"format": 1, "tests": doc["tests"],
                 "by_file": {rel: pickle.dumps(v, protocol=4) for rel, v in by_file.items()}}, fh, protocol=4)
