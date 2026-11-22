"""Weak-mutation data for test_execute_tpcds per TPC-DS query (tools/tpcds_cmp_plugin.py on
top of tools/cmp_plugin.py), at the pinned commit.

For each rewritten comparison or and/or site that the test evaluated:
  ran      the queries (indices into tpc-ds.sql's cases) that evaluated it
  differs  the queries where the neighbouring operator would have given another result
  other    whether the test evaluated it outside any query (pool start, initializer), and
           whether it differed there
The selector uses it (Selector.tpcds_plan) to replay only the queries where a swap to that
neighbour can change anything, and only for sites whose evaluating queries cover every query
whose recorded line coverage reaches the line (code that runs once per process is
attributed to the first query here, so it gets no narrowing).

usage: python build_tpcds_cmp.py <sqlglot checkout> <out.pkl.gz>
"""
import gzip
import json
import os
import pickle
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
out_path = sys.argv[2]
here = Path(__file__).resolve().parent
TARGET = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
out_json = "/tmp/tpcds_cmp.json"
if os.path.exists(out_json):
    os.remove(out_json)
env = dict(os.environ, CMP_OUT="/tmp/cmp_unused.json", TPCDS_CMP_OUT=out_json,
           PYTHONPATH=f"{here}{os.pathsep}{root}", SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
proc = subprocess.run([sys.executable, "-m", "pytest", TARGET, "-q", "-p", "no:cacheprovider",
                       "-p", "cmp_plugin", "-p", "tpcds_cmp_plugin", "--timeout", "3000"],
                      cwd=root, env=env, capture_output=True, text=True)
print("instrumented run:", (proc.stdout.strip().splitlines() or ["?"])[-1])
d = json.load(open(out_json))
if d["failed"] or proc.returncode != 0:
    print(proc.stdout[-3000:])
    sys.exit("the instrumented test must pass at the pinned commit")
sites = [tuple(s) for s in d["sites"]]


def split(ctxs):
    qs, other = set(), False
    for c in ctxs:
        if c.startswith("Q") and c[1:].isdigit():
            qs.add(int(c[1:]))
        else:
            other = True
    return qs, other


out = {}
for idx in set(d["ran"]) | set(d["differs"]):
    rel, line, col, op = sites[int(idx)]
    ran, other_ran = split(d["ran"].get(idx, ()))
    differs, other_differs = split(d["differs"].get(idx, ()))
    out[(rel, line, col)] = {"op": op, "ran": frozenset(ran | differs), "differs": frozenset(differs),
                             "other_ran": other_ran or other_differs, "other_differs": other_differs}
nq = 1 + max((q for v in out.values() for q in v["ran"]), default=-1)
with gzip.open(out_path, "wb", compresslevel=9) as fh:
    pickle.dump({"format": 1, "test_id": TARGET, "queries": nq, "sites": out}, fh, protocol=4)
print(f"{len(out)} sites evaluated by the test over {nq} queries; "
      f"{sum(1 for v in out.values() if v['differs'])} differ in some query, "
      f"{sum(1 for v in out.values() if v['other_differs'])} outside any query")
