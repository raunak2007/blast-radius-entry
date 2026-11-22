"""Per-case coverage of the slow fixture-loop tests (build-time / offline tool).

For a test T that loops over the cases of fixture F (maps/cases.json), trace T
restricted to one case at a time (selector/cases.py: the loaders yield only that
ordinal, other fixtures of T yield nothing), each run in a fresh child forked
from a zygote that never imported sqlglot or the tests, exactly like
tools/trace_tests.py traces whole tests. A record therefore holds everything a
restricted run of T needs: imports, class fixtures, code outside the loop, and
that one case, with lazily built caches attributed to every case that needs them.

Also measures, without tracing, the CPU seconds of each restricted run (--measure),
which the planner uses to price a restricted verification.

Usage: python tools/trace_cases.py <sqlglot checkout> <cases.json> <out dir> --tests id,id [--jobs N] [--measure]
Records: <out dir>/<k>.json with "test", "fixture", "ordinal", "pseudo" (pseudo-test id).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import trace_tests  # noqa: E402
from selector import cases  # noqa: E402


def pseudo_id(test, fixture, ordinal):
    return f"{test}[{fixture}#{ordinal}]"


def restricted_run(test, allowed):
    def run():
        import tests.helpers as helpers

        cases.install(helpers)
        cases.ALLOWED.clear()
        cases.ALLOWED.update(allowed)
        from selector.runner import run_tests

        failed, durations = run_tests([test])
        return {"failed": test in failed, "duration": round(durations.get(test, 0.0), 4),
                "body_cpu": round(getattr(run_tests, "last_cpu", {}).get(test, 0.0), 4)}

    return run


def measure_child(run, out_path):
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            started = time.monotonic()
            status = run()
            usage = resource.getrusage(resource.RUSAGE_SELF)
            with open(out_path, "w") as handle:
                json.dump({"status": status, "wall": time.monotonic() - started, "cpu": status["body_cpu"],
                           "process_cpu": usage.ru_utime + usage.ru_stime}, handle)
        except BaseException as error:  # noqa: BLE001
            print(f"measure child crashed: {error!r}", file=sys.stderr)
            code = 1
        finally:
            os._exit(code)
    return pid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("cases_json")
    ap.add_argument("out")
    ap.add_argument("--tests", required=True)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--warm", action="store_true",
                    help="trace/measure cases in children of a zygote that already imported the tests "
                         "(the cold 'outside' records must exist; augment_maps unions them in)")
    ap.add_argument("--outside-only", action="store_true", help="only the cold zero-case records")
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

    if o.warm:
        # Import the loaders (wrapped) and the test modules once in the zygote; each
        # case then runs in a fork that starts from this state. Import-time code and
        # anything the zygote ran are in the cold 'outside' record of the test.
        import importlib

        import tests.helpers as helpers

        cases.install(helpers)
        for test in o.tests.split(","):
            importlib.import_module(test.split("::")[0][:-3].replace("/", "."))
    work = []
    for test in o.tests.split(","):
        fixtures = table[test]
        for fixture, kind in fixtures:
            text = open(os.path.join(root, "tests", "fixtures", fixture), "rb").read().decode("utf-8")
            n = len(cases.pair_cases(text) if kind == "pairs" else cases.line_cases(text))
            for k in range(n if not o.outside_only else 0):
                allowed = {os.path.normpath(f): frozenset() for f, _ in fixtures}
                allowed[os.path.normpath(fixture)] = frozenset([k])
                work.append((test, fixture, k, allowed))
        # the test with every loop empty: what runs outside the cases (always cold)
        if not o.warm:
            work.append((test, None, -1, {os.path.normpath(f): frozenset() for f, _ in fixtures}))
    suffix = ".cpu.json" if o.measure else ".json"
    names = {}
    pending = []
    for i, (test, fixture, k, allowed) in enumerate(work):
        name = hashlib.sha1(repr((test, fixture, k)).encode()).hexdigest()[:16]
        names[name] = (test, fixture, k)
        if not os.path.exists(os.path.join(out, name + suffix)):
            pending.append((name, test, fixture, k, allowed))
    print(f"{len(work)} cases, {len(pending)} to do", file=sys.stderr)
    running = {}
    done = 0
    while pending or running:
        while pending and len(running) < o.jobs:
            name, test, fixture, k, allowed = pending.pop(0)
            tmp = os.path.join(out, name + suffix + ".tmp")
            run = restricted_run(test, allowed)
            pid = measure_child(run, tmp) if o.measure else trace_tests.fork_and_trace(root, run, tmp)
            running[pid] = (name, test, fixture, k)
        pid, status = os.wait()
        if pid not in running:
            continue
        name, test, fixture, k = running.pop(pid)
        tmp = os.path.join(out, name + suffix + ".tmp")
        if status == 0 and os.path.exists(tmp):
            record = json.load(open(tmp))
            record.update({"test": test, "fixture": fixture, "ordinal": k, "warm": bool(o.warm),
                           "pseudo": pseudo_id(test, fixture, k) if fixture else f"{test}[outside]"})
            json.dump(record, open(os.path.join(out, name + suffix), "w"))
            os.remove(tmp)
            if record["status"].get("failed"):
                print(f"FAILED in clean run: {record['pseudo']}", file=sys.stderr)
        else:
            print(f"case run failed: {test} {fixture} {k} (status {status})", file=sys.stderr)
        done += 1
        if done % 25 == 0:
            print(f"{done} done, {len(pending)} pending", file=sys.stderr)


if __name__ == "__main__":
    main()
