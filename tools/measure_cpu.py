"""Measure per-test CPU seconds as our verifier runs them (in-process, pools made
synchronous), one fresh process per test file. Dev tool for cost calibration.

Usage: python tools/measure_cpu.py <sqlglot checkout> <tests.json> <out.json>
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

CHILD = r"""
import json, sys, time, os
sys.path.insert(0, %(proj)r)
sys.path.insert(0, %(repo)r)
os.chdir(%(repo)r)
from selector import shim; shim.install()
from selector.runner import run_tests
ids = json.loads(sys.argv[1])
t0 = time.process_time()
failed, durations = run_tests(ids, limit_of=lambda n: 600)
out = {"cpu": run_tests.last_cpu, "wall": durations, "failed": sorted(failed), "total": time.process_time() - t0}
print("RESULT" + json.dumps(out))
"""


def main():
    repo, tests_json, out_path = sys.argv[1:4]
    tests = json.load(open(tests_json))["tests"]
    by_file = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t["id"])
    results = json.load(open(out_path)) if os.path.exists(out_path) else {}
    env = dict(os.environ, SKIP_INTEGRATION="0")
    for f, ids in by_file.items():
        if all(i in results for i in ids):
            continue
        code = CHILD % {"proj": os.path.dirname(HERE), "repo": os.path.abspath(repo)}
        proc = subprocess.run([sys.executable, "-c", code, json.dumps(ids)], capture_output=True, text=True, env=env)
        line = [l for l in proc.stdout.splitlines() if l.startswith("RESULT")]
        if not line:
            print("failed for", f, proc.stderr[-2000:], file=sys.stderr)
            continue
        data = json.loads(line[0][6:])
        for i in ids:
            results[i] = {"cpu": data["cpu"].get(i), "wall": data["wall"].get(i), "failed": i in data["failed"]}
        json.dump(results, open(out_path, "w"))
        print(f, len(ids), round(sum(v or 0 for v in data["cpu"].values()), 2), "failed", data["failed"], file=sys.stderr)


if __name__ == "__main__":
    main()
