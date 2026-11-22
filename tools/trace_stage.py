"""Outer coverage and optimize() fingerprints per TPC-DS case (build-time tool).

For each case of tests/test_executor.py::TestExecutor::test_execute_tpcds, in a
child forked from a zygote that never imported sqlglot or the tests (like
tools/trace_cases.py, test restricted to that one case):

* --mode trace: record the lines and function entries run OUTSIDE calls of
  `sqlglot.executor.optimize` (everything the case does except optimizing), and
  the fingerprints (selector/stage.py) of each call's input, output, and input
  after the call;
* --mode measure: run the case the way the verifier checks it (stopped right
  after optimize returns) and record its body CPU and the same fingerprints.

Run the two modes with different PYTHONHASHSEED values: a case whose
fingerprints differ between them is not deterministic and is never checked this
way.

Usage: PYTHONHASHSEED=1 python tools/trace_stage.py <sqlglot> <cases.json> <out dir> --mode trace
       PYTHONHASHSEED=2 python tools/trace_stage.py <sqlglot> <cases.json> <out dir> --mode measure
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import trace_tests  # noqa: E402
from selector import cases, stage  # noqa: E402

TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


def run_case(root, fixture, k, mode, out_path):
    import importlib

    record = {"calls": [], "func_lines": {}, "mod_lines": {}, "entered": {}}
    inside = [0]
    root_slash = root + os.sep
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    if mode == "trace":
        mon.use_tool_id(tool, "blast-radius-stage")
        CO_OPTIMIZED = 0x1

        def on_line(code, line):
            if inside[0]:
                return None  # not recorded, and kept enabled for a later run outside
            filename = code.co_filename
            if filename.startswith(root_slash):
                rel = filename[len(root_slash):]
                target = record["func_lines"] if code.co_flags & CO_OPTIMIZED else record["mod_lines"]
                target.setdefault(rel, set()).add(line)
            return mon.DISABLE

        def on_start(code, offset):
            if inside[0]:
                return None
            filename = code.co_filename
            if filename.startswith(root_slash):
                record["entered"].setdefault(filename[len(root_slash):], set()).add(
                    f"{code.co_firstlineno}:{code.co_qualname}")
            return mon.DISABLE

        mon.register_callback(tool, mon.events.LINE, on_line)
        mon.register_callback(tool, mon.events.PY_START, on_start)
        mon.set_events(tool, mon.events.LINE | mon.events.PY_START)

    import tests.helpers as helpers

    cases.install(helpers)
    cases.ALLOWED.clear()
    if k >= 0:
        cases.ALLOWED.update({os.path.normpath(fixture): frozenset([k])})
    executor = importlib.import_module("sqlglot.executor")
    original = executor.optimize

    def recording(*args, **kwargs):
        before = stage.fingerprint(args[0] if args else kwargs.get("expression"))
        inside[0] += 1
        try:
            result = original(*args, **kwargs)
        finally:
            inside[0] -= 1
        after = stage.fingerprint(result)
        after_input = stage.fingerprint(args[0] if args else kwargs.get("expression"))
        record["calls"].append([before, after, after_input])
        if mode in ("measure", "measure_all"):
            raise stage.StageDone()
        return result

    executor.optimize = recording
    from selector.runner import run_tests

    started = time.monotonic()
    failed, _ = run_tests([TEST])
    if mode == "trace":
        mon.set_events(tool, 0)
    record["failed"] = TEST in failed
    record["body_cpu"] = round(getattr(run_tests, "last_cpu", {}).get(TEST, 0.0), 4)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    record["process_cpu"] = round(usage.ru_utime + usage.ru_stime, 4)
    record["wall"] = round(time.monotonic() - started, 4)
    for key in ("func_lines", "mod_lines", "entered"):
        record[key] = {f: sorted(v) for f, v in record[key].items()}
    record.update({"test": TEST, "fixture": fixture, "ordinal": k, "mode": mode,
                   "hashseed": os.environ.get("PYTHONHASHSEED")})
    with open(out_path, "w") as handle:
        json.dump(record, handle)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("cases_json")
    ap.add_argument("out")
    ap.add_argument("--mode", choices=("trace", "measure", "measure_all"), required=True)
    ap.add_argument("--jobs", type=int, default=2)
    o = ap.parse_args()
    root = os.path.realpath(o.repo)
    out = os.path.abspath(o.out)
    os.makedirs(out, exist_ok=True)
    table = json.load(open(o.cases_json))["tests"]
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    trace_tests.preload()
    (fixture, kind), = table[TEST]
    text = open(os.path.join(root, "tests", "fixtures", fixture), "rb").read().decode("utf-8")
    n = len(cases.pair_cases(text))
    pending = [k for k in range(n) if not os.path.exists(os.path.join(out, f"{o.mode}_{k}.json"))]
    if o.mode == "measure_all":  # every case in one run, stopped after each optimize()
        pending = [-1] if not os.path.exists(os.path.join(out, "measure_all_-1.json")) else []
    print(f"{n} cases, {len(pending)} to do ({o.mode})", file=sys.stderr)
    running = {}
    while pending or running:
        while pending and len(running) < o.jobs:
            k = pending.pop(0)
            path = os.path.join(out, f"{o.mode}_{k}.json")
            pid = os.fork()
            if pid == 0:
                code = 0
                try:
                    devnull = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(devnull, 1)
                    run_case(root, fixture, k, o.mode, path + ".tmp")
                    os.rename(path + ".tmp", path)
                except BaseException as error:  # noqa: BLE001
                    print(f"case {k} failed: {error!r}", file=sys.stderr)
                    code = 1
                finally:
                    os._exit(code)
            running[pid] = k
        pid, status = os.wait()
        k = running.pop(pid, None)
        if status != 0:
            print(f"case {k}: status {status}", file=sys.stderr)


if __name__ == "__main__":
    main()
