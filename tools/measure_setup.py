"""Measure the CPU cost of class-level fixtures (setUpClass) as our verifier
pays it: run one cheap test of each class with a fixture, minus that test.
Writes {"path::Class": seconds}. Usage: measure_setup.py <checkout> <tests.json> <out> [scale]"""
import json, os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
CHILD = r"""
import json, sys, time, os
sys.path.insert(0, %(proj)r); sys.path.insert(0, %(repo)r); os.chdir(%(repo)r)
from selector import shim; shim.install()
import importlib
mod = sys.argv[1]
importlib.import_module(mod)
from selector.runner import run_tests
t0 = time.process_time()
failed, d = run_tests([sys.argv[2]], limit_of=lambda n: 600)
total = time.process_time() - t0
print("RESULT" + json.dumps({"total": total, "test": sum(run_tests.last_cpu.values())}))
"""
repo, tests_json, out = sys.argv[1:4]
scale = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
tests = json.load(open(tests_json))["tests"]
classes = {}
for t in tests:
    path, cls, _ = t["id"].split("::")
    src = open(os.path.join(repo, path)).read()
    if "def setUpClass" in src or "def setUpModule" in src:
        key = f"{path}::{cls}"
        if key not in classes or t["seconds"] < classes[key]["seconds"]:
            classes[key] = t
result = {}
env = dict(os.environ, SKIP_INTEGRATION="0")
for key, t in classes.items():
    mod = t["file"][:-3].replace("/", ".")
    code = CHILD % {"proj": os.path.dirname(HERE), "repo": os.path.abspath(repo)}
    best = None
    for _ in range(2):
        proc = subprocess.run([sys.executable, "-c", code, mod, t["id"]], capture_output=True, text=True, env=env)
        line = [l for l in proc.stdout.splitlines() if l.startswith("RESULT")]
        if line:
            data = json.loads(line[0][6:])
            cost = max(0.0, data["total"] - data["test"])
            best = cost if best is None else min(best, cost)
    if best is not None:
        result[key] = round(best / scale, 3)
        print(key, result[key], file=sys.stderr)
json.dump(result, open(out, "w"), indent=1)
