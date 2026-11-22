"""Pack build_import_users.py's JSON into import_users.pkl.gz for the selector.

usage: python pack_import_users.py <import_users.json> <out.pkl.gz>

cross: {rel: {line: (affects every test, files of the affected modules, tests that import
them while running)}}; test_modules: the test modules whose own import runs sqlglot function
bodies (the import-time state check imports them too).
"""
import gzip
import json
import pickle
import sys

src, out = sys.argv[1:3]
d = json.load(open(src))
cross = {rel: {int(ln): (v["all"], tuple(v["files"]), tuple(v["tests"])) for ln, v in per.items()}
         for rel, per in d["cross"].items()}
mods = sorted({m for lines in d["users"].values() for ms in lines.values() for m in ms if m.startswith("tests")})
with gzip.open(out, "wb", compresslevel=9) as fh:
    pickle.dump({"version": 2, "cross": cross, "test_modules": mods}, fh, protocol=4)
print(f"{sum(len(v) for v in cross.values())} lines; test modules {mods}", file=sys.stderr)
