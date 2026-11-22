"""Merge build_fixture_cov.py outputs into fixtures.pkl.gz for the selector.

usage: python pack_fixtures.py <out.pkl.gz> <cov.pkl> [<cov.pkl> ...]

Each test is pickled separately so a run unpickles only the tests its commits select.
Development only.
"""
import gzip
import pickle
import sys

out_path, inputs = sys.argv[1], sys.argv[2:]
by_test = {}
for path in inputs:
    with open(path, "rb") as fh:
        data = pickle.load(fh)
    for tid, e in sorted(data.items()):
        by_test[tid] = pickle.dumps(e, protocol=4)
        print(f"{tid}: {e['entries']} entries, {sum(len(v) for v in e['lines'].values())} lines, "
              f"{sum(len(v) for v in e['base'].values())} base lines, {len(by_test[tid]) // 1024} KB")
with gzip.open(out_path, "wb", compresslevel=9) as fh:
    pickle.dump({"format": 1, "by_test": by_test}, fh, protocol=4)
