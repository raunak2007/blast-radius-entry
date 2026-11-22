"""Per fixture entry of the fixture-driven tests: the sqlglot lines its processing runs.

usage (cwd = the pinned sqlglot checkout): python build_fixture_cov.py <out.pkl> [module ...]

A fixture-driven test loops over the entries of a .sql fixture (tests.helpers.load_sql_fixtures
and load_sql_fixture_pairs) and checks each one. For every such test this records, per loader
call k and entry i:

  - the lines run while that entry is current in one ordinary run of the whole test (from
    the moment the loader yields it until the next yield, and again whenever one of its
    futures is resolved), and
  - every line a fresh process runs for the test when the loaders yield only that entry
    (class and test setup included), so code that only runs the first time something is
    used is in the coverage of every entry that needs it.

Output per test: its loader calls, the CPU of a plain run, "base" (lines of the test run with
every loader yielding nothing, plus anything the ordinary run did outside every entry: all
entries depend on these) and "lines" {file: {line: entries}} (bit i = the i-th entry, counting
through the loader calls in order).

Pool work runs in the same process (arguments and results pickled both ways, as a pool does),
so it is traced too. Only tests that pass on their own, entry by entry, are kept.
Development tool; the selector reads the packed output.
"""
import importlib
import io
import os
import pickle
import sys
import time
import unittest
from concurrent.futures import Future

ROOT = os.getcwd()
PKG = os.path.join(ROOT, "sqlglot") + os.sep
out_path = sys.argv[1]
modules = sys.argv[2:] or ["tests.test_optimizer", "tests.test_transpile", "tests.test_serde"]
sys.path.insert(0, ROOT)
os.environ["SKIP_INTEGRATION"] = "0"
LOADERS = ("load_sql_fixtures", "load_sql_fixture_pairs")
mon = sys.monitoring
TOOL = mon.COVERAGE_ID


class SyncPool:
    """Stands in for the class's ProcessPoolExecutor: same calls, same pickling, this process."""

    def __init__(self, *args, **kwargs):
        pass

    def submit(self, fn, *args, **kwargs):
        future = Future()
        future.fx_ctx = STATE["ctx"]
        try:
            fn2, args2, kwargs2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            future.set_result(pickle.loads(pickle.dumps(fn2(*args2, **kwargs2))))
        except BaseException as error:  # noqa: BLE001
            future.set_exception(error)
        real_result = future.result

        def result(timeout=None):
            switch(future.fx_ctx)
            return real_result(timeout)

        future.result = result
        return future

    def shutdown(self, *args, **kwargs):
        pass


STATE = {"ctx": None, "lines": {}, "calls": [], "only": None}


def switch(ctx):
    if ctx != STATE["ctx"]:
        STATE["ctx"] = ctx
        mon.restart_events()


def on_line(code, line):
    name = code.co_filename
    if name.startswith(PKG):
        STATE["lines"].setdefault(STATE["ctx"], set()).add((name[len(ROOT) + 1:], line))
    return mon.DISABLE


def wrap_loaders(mod):
    originals = {name: getattr(mod, name) for name in LOADERS if hasattr(mod, name)}

    def make(name, original):
        def loader(filename):
            k = len(STATE["calls"])
            entries = list(original(filename))
            STATE["calls"].append((name, filename, len(entries)))
            only = STATE["only"]
            for i, entry in enumerate(entries):
                if only is not None and (k, i) != only:
                    continue
                switch((k, i))
                yield entry
            switch(None)
        return loader

    for name, original in originals.items():
        setattr(mod, name, make(name, original))


def run_one(mod, cls_name, meth, only, trace):
    """In a forked child: run one test; returns (passed, calls, lines by context, cpu)."""
    STATE.update(ctx=None, lines={}, calls=[], only=only)
    if hasattr(mod, "ProcessPoolExecutor"):
        mod.ProcessPoolExecutor = SyncPool
    wrap_loaders(mod)
    cls = getattr(mod, cls_name)
    result = unittest.TestResult()
    started = time.process_time()
    if trace:
        mon.use_tool_id(TOOL, "fixture-cov")
        mon.register_callback(TOOL, mon.events.LINE, on_line)
        mon.set_events(TOOL, mon.events.LINE)
    try:
        unittest.TestSuite([cls(meth)]).run(result)
    finally:
        if trace:
            mon.set_events(TOOL, 0)
    passed = result.wasSuccessful() and result.testsRun == 1 and not result.skipped
    return passed, STATE["calls"], STATE["lines"], time.process_time() - started


def forked(fn, *args):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            data = pickle.dumps(fn(*args))
            with os.fdopen(w, "wb") as fh:
                fh.write(data)
        except BaseException:  # noqa: BLE001
            pass
        os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as fh:
        data = fh.read()
    os.waitpid(pid, 0)
    return pickle.loads(data) if data else None


shard = os.environ.get("FX_SHARD")  # "i/n": only every n-th fixture-driven candidate, for running in parallel
shard_i, shard_n = (int(x) for x in shard.split("/")) if shard else (0, 1)
only_tests = set(filter(None, os.environ.get("FX_ONLY", "").split(",")))
out = {}
t0 = time.time()
seen = 0
for modname in modules:
    mod = importlib.import_module(modname)
    rel = modname.replace(".", "/") + ".py"
    for cls_name, cls in sorted(vars(mod).items()):
        if not (isinstance(cls, type) and issubclass(cls, unittest.TestCase) and cls.__module__ == modname):
            continue
        for meth in sorted(m for m in dir(cls) if m.startswith("test")):
            tid = f"{rel}::{cls_name}::{meth}"
            seen += 1
            if seen % shard_n != shard_i or (only_tests and meth not in only_tests):
                continue
            probe = forked(run_one, mod, cls_name, meth, None, False)
            if not probe or not probe[1]:
                continue  # no fixture loader used (or it could not run)
            if not probe[0]:
                print(f"{tid}: fails on its own, skipped", flush=True)
                continue
            warm = forked(run_one, mod, cls_name, meth, None, True)
            empty = forked(run_one, mod, cls_name, meth, (-1, -1), True)
            if not warm or not warm[0] or not empty or not empty[0]:
                print(f"{tid}: traced run failed, skipped", flush=True)
                continue
            calls = warm[1]
            offsets, total = [], 0
            for _, _, count in calls:
                offsets.append(total)
                total += count
            base = {}
            for r_, line in set().union(*empty[2].values()) | warm[2].get(None, set()) if (empty[2] or warm[2]) else ():
                base.setdefault(r_, set()).add(line)
            lines = {}

            def fold(ctx, covered):
                bit = 1 << (offsets[ctx[0]] + ctx[1])
                for r_, line in covered:
                    if line in base.get(r_, ()):
                        continue
                    per = lines.setdefault(r_, {})
                    per[line] = per.get(line, 0) | bit

            for ctx, covered in warm[2].items():
                if ctx is not None:
                    fold(ctx, covered)
            ok, n, cpu = True, 0, 0.0
            for k, (_, _, count) in enumerate(calls):
                for i in range(count):
                    got = forked(run_one, mod, cls_name, meth, (k, i), True)
                    if not got or not got[0] or got[1] != calls:
                        ok = False
                        break
                    fold((k, i), set().union(*got[2].values()) if got[2] else set())
                    cpu += got[3]
                    n += 1
                if not ok:
                    break
            if not ok:
                print(f"{tid}: entry {(k, i)} fails alone or changes the calls, skipped", flush=True)
                continue
            out[tid] = {"calls": [tuple(c) for c in calls], "entries": total, "full_ms": int(round(probe[3] * 1000)),
                        "base": {r_: frozenset(v) for r_, v in base.items()}, "lines": lines}
            print(f"{tid}: {len(calls)} loader calls, {n} entries, full {probe[3]:.2f}s, "
                  f"entries {cpu:.1f}s traced [{time.time() - t0:.0f}s]", flush=True)
            with open(out_path, "wb") as fh:
                pickle.dump(out, fh, protocol=pickle.HIGHEST_PROTOCOL)
print(len(out), "tests recorded")
