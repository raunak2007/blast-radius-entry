"""Repack the selector's larger data files so a run unpickles only what its commits need.

usage: python build_lazy.py <blast_map.pkl.gz> <dictkeys in> <dictkeys out> <operands in> <operands out> <dictreads out>

- dictkeys: test-id lists become test masks in blast_map's order, and each dict name's
  per-key reads are pickled separately (the selector unpickles one name at a time).
- operands (main's tools/build_operands.py output): comparison sites and dict-literal reads
  are grouped per source file and pickled separately; enums stay whole.
Same information, different packing. Development only.
"""
import gzip
import pickle
import sys

map_path, dk_in, dk_out, op_in, op_out, dr_out = sys.argv[1:7]
with gzip.open(map_path, "rb") as fh:
    doc = pickle.load(fh)
bit = {t: 1 << i for i, t in enumerate(doc["tests"])}


def mask(tids):
    m = 0
    for t in tids:
        m |= bit.get(t, 0)
    return m


def dump(obj, path):
    with gzip.open(path, "wb", compresslevel=9) as fh:
        pickle.dump(obj, fh, protocol=4)


with gzip.open(dk_in, "rb") as fh:
    d = pickle.load(fh)
if d.get("format") == 3:
    out = d  # already packed
else:
    out = {"format": 3, "tests": doc["tests"], "tracked": d["tracked"], "unsafe": d["unsafe"],
           "key_level": d.get("key_level", {}), "key_level_chars": d.get("key_level_chars", {}),
           "full_tests": {a: mask(t) for a, t in d["full_tests"].items()},
           "keys": {a: pickle.dumps({k: mask(t) for k, t in per.items()}, protocol=4) for a, per in d["keys"].items()}}
    if "keys_owned" in d:
        out["keys_owned"] = {a: pickle.dumps({o: {k: mask(t) for k, t in per.items()} for o, per in owned.items()},
                                             protocol=4)
                             for a, owned in d["keys_owned"].items()}
        out["derived"] = d["derived"]
dump(out, dk_out)

with gzip.open(op_in, "rb") as fh:
    o = pickle.load(fh)
assert o["tests"] == doc["tests"], "operand masks are in another test order"
sites, dicts = {}, {}
for (rel, line, col), site in o["sites"].items():
    sites.setdefault(rel, {})[(line, col)] = site
for (rel, line, col), entry in o["dicts"].items():
    dicts.setdefault(rel, {})[(line, col)] = entry
dump({"format": 3, "tests": doc["tests"], "enums": o["enums"],
      "sites_by_file": {rel: pickle.dumps(v, protocol=4) for rel, v in sites.items()}}, op_out)
dump({"format": 3, "tests": doc["tests"],
      "dicts_by_file": {rel: pickle.dumps(v, protocol=4) for rel, v in dicts.items()}}, dr_out)
print(f"{len(out['keys'])} dict names; {len(sites)} files of comparison sites; {len(dicts)} files of dict literals",
      file=sys.stderr)
