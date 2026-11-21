"""Merge operand_plugin outputs into operands.pkl.gz, with test masks in blast_map's order.

usage: python build_operands.py <operand pickle prefix> <blast_map.pkl.gz> <out.pkl.gz>

Per comparison site (rel, line, col): its operator, each distinct operand sample with
the mask of tests that saw it, the mask of tests we couldn't describe there ("over"),
and whether it ran while modules were imported. Built only from the pinned commit's
own test run.
"""
import glob
import gzip
import pickle
import sys

prefix, map_path, out_path = sys.argv[1:4]
with gzip.open(map_path, "rb") as fh:
    doc = pickle.load(fh)
bit = {t: 1 << i for i, t in enumerate(doc["tests"])}
ALL = (1 << len(doc["tests"])) - 1


def ctx_mask(ctx):
    if ctx.startswith("CLASS:"):
        return doc["test_classes"].get(ctx[6:], 0)
    if ctx.startswith("FILE:"):
        return doc["test_files"].get(ctx[5:], 0)
    return bit.get(ctx, 0)


sites, enums, dicts = {}, {}, {}
unmatched = set()
for path in sorted(glob.glob(prefix + "*")):
    with open(path, "rb") as fh:
        part = pickle.load(fh)
    for name, info in part["enums"].items():
        enums.setdefault(name, info)
    for site in part.get("dict_sites", []):  # wrapped, even if nothing read it
        dicts.setdefault(tuple(site), {"reads": {}, "bulk": 0, "import_keys": set(), "import_bulk": False})
    for did, per in part.get("dict_reads", {}).items():
        d = dicts.setdefault(tuple(part["dict_sites"][did]),
                             {"reads": {}, "bulk": 0, "import_keys": set(), "import_bulk": False})
        for ctx, keys in per.items():
            if ctx == "IMPORT":
                if keys is None:
                    d["import_bulk"] = True
                else:
                    d["import_keys"] |= keys
                continue
            m = ctx_mask(ctx)
            if keys is None:
                d["bulk"] |= m
                continue
            for k in keys:
                d["reads"][k] = d["reads"].get(k, 0) | m
    for did, ctxs in part.get("dict_bulk", {}).items():
        d = dicts.setdefault(tuple(part["dict_sites"][did]),
                             {"reads": {}, "bulk": 0, "import_keys": set(), "import_bulk": False})
        for ctx in ctxs:
            if ctx == "IMPORT":
                d["import_bulk"] = True
            else:
                d["bulk"] |= ctx_mask(ctx)
    for sid, per in part["records"].items():
        rel, line, col, op = part["sites"][sid]
        key = (rel, line, col)
        site = sites.setdefault(key, {"op": op, "samples": {}, "over": 0, "import": False})
        if site["op"] != op:
            raise SystemExit(f"operator mismatch at {key}")
        for ctx, samples in per.items():
            if ctx == "IMPORT":
                site["import"] = True
                continue
            m = ctx_mask(ctx)
            if not m:
                unmatched.add(ctx)
                continue
            if samples is None:
                site["over"] |= m
                continue
            for s in samples:
                site["samples"][s] = site["samples"].get(s, 0) | m

out = {}
for key, site in sites.items():
    samples = sorted(site["samples"].items(), key=repr)
    if len(samples) > 400:  # too varied to be worth re-evaluating; treat every user as unknown
        over = site["over"]
        for _, m in samples:
            over |= m
        samples = []
        site["over"] = over
    out[key] = {"op": site["op"], "samples": samples, "over": site["over"], "import": site["import"]}

print(f"{len(out)} sites, {sum(len(s['samples']) for s in out.values())} samples, "
      f"{sum(s['import'] for s in out.values())} run at import, {len(unmatched)} unmatched contexts; "
      f"{len(dicts)} dicts read, {sum(d['import_bulk'] for d in dicts.values())} read in bulk at import",
      file=sys.stderr)
with gzip.open(out_path, "wb", compresslevel=9) as fh:
    pickle.dump({"sites": out, "enums": enums, "dicts": dicts, "tests": doc["tests"]}, fh, protocol=4)
