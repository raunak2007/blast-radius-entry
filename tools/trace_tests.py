"""Per-test coverage tracer (build-time / offline tool, never used at decision time).

For every test in tests.json, fork a fresh child from a zygote that has only
third-party libraries loaded (never sqlglot or the tests), and record while the
test's module is imported and the test runs:

* every (file, line) executed in the repository, via sys.monitoring LINE events;
* every repository file opened (audit hook "open"), and every directory listed
  outside the import system (audit hooks "os.listdir"/"os.scandir");
* subprocesses started (audit hook "subprocess.Popen"); a `python -c CODE`
  child is re-run under the tracer and its lines are merged into the test;
* pass/fail, wall time and CPU time of the child.

Because each test starts from an interpreter that has not imported sqlglot,
import-time code and lazily built caches are attributed to every test that
needs them, which is what makes the coverage a sound basis for selection.

Usage: python tools/trace_tests.py <sqlglot checkout> <tests.json> <out dir> [--jobs N] [--only substr]
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

PRELOAD = [
    "unittest", "unittest.mock", "json", "datetime", "decimal", "sqlite3", "gc",
    "tracemalloc", "tempfile", "pathlib", "pickle", "csv", "gzip", "itertools",
    "functools", "collections", "importlib", "pkgutil", "runpy", "ast", "doctest",
    "subprocess", "concurrent.futures", "multiprocessing", "zoneinfo", "weakref",
    "typing", "typing_extensions", "re", "math", "logging", "enum", "copy", "string",
    "inspect", "threading", "dataclasses", "warnings", "types", "textwrap",
    "statistics", "numbers", "dateutil", "dateutil.parser", "pytz", "numpy", "pandas",
    "pandas.testing", "duckdb", "setuptools", "setuptools.command.build_ext", "pytest",
]


def preload():
    for name in PRELOAD:
        try:
            __import__(name)
        except Exception as error:  # noqa: BLE001
            print(f"preload {name} failed: {error}", file=sys.stderr)
    from selector import shim

    shim.install()


def child_trace(root, run, out_path):
    """In a forked child: install tracers, call run(), dump the record to out_path."""
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    mon.use_tool_id(tool, "blast-radius-trace")
    # Lines run by function code (CO_OPTIMIZED: defs, lambdas) and by module or
    # class-body code (import time) are kept apart, and so are function entries:
    # a LINE event never fires for a body that shares the def's line (lambdas,
    # one-line defs), so PY_START is what shows those were called.
    func_lines: dict[str, set[int]] = {}
    mod_lines: dict[str, set[int]] = {}
    entered: dict[str, set[str]] = {}
    root_slash = root + os.sep
    CO_OPTIMIZED = 0x1

    def on_line(code, line):
        filename = code.co_filename
        if filename.startswith(root_slash):
            rel = filename[len(root_slash):]
            target = func_lines if code.co_flags & CO_OPTIMIZED else mod_lines
            bucket = target.get(rel)
            if bucket is None:
                bucket = target[rel] = set()
            bucket.add(line)
        return mon.DISABLE

    def on_start(code, offset):
        filename = code.co_filename
        if filename.startswith(root_slash):
            rel = filename[len(root_slash):]
            bucket = entered.get(rel)
            if bucket is None:
                bucket = entered[rel] = set()
            bucket.add(f"{code.co_firstlineno}:{code.co_qualname}")
        return mon.DISABLE

    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.register_callback(tool, mon.events.PY_START, on_start)

    opens: set[str] = set()
    lists: set[str] = set()
    popens: list = []

    def rel_of(path):
        try:
            path = os.fspath(path)
        except TypeError:
            return None
        if isinstance(path, bytes):
            path = path.decode("utf-8", "replace")
        if not isinstance(path, str):
            return None
        path = os.path.abspath(path)
        if path == root:
            return "."
        if path.startswith(root_slash):
            return path[len(root_slash):]
        return None

    def in_import_system():
        frame = sys._getframe(2)
        while frame is not None:
            name = frame.f_code.co_filename
            if name.startswith("<frozen importlib") or "importlib/_bootstrap" in name:
                return True
            frame = frame.f_back
        return False

    def audit(event, args):
        if event == "open":
            rel = rel_of(args[0])
            if rel is not None and not rel.endswith(".pyc"):
                if rel.endswith(".py") and in_import_system():
                    return
                opens.add(rel)
        elif event in ("os.listdir", "os.scandir"):
            rel = rel_of(args[0] if args[0] is not None else ".")
            if rel is not None and not in_import_system():
                lists.add(rel)
        elif event == "subprocess.Popen":
            try:
                popens.append([os.fspath(args[0]), [str(a) for a in args[1]]])
            except Exception:  # noqa: BLE001
                popens.append(["?", [repr(args)]])

    # Record which sqlglot classes get instantiated: wrap every Python __init__
    # defined in a sqlglot module right after the module executes; the wrapper
    # notes type(self), so subclasses relying on a base __init__ are seen too.
    # Instances left in module globals at import (singletons) count as well.
    import functools
    import importlib.abc
    import importlib.machinery
    import types

    instantiated: set = set()
    wrapped: set = set()

    def wrap_class(cls):
        if id(cls) in wrapped:
            return
        wrapped.add(id(cls))
        init = cls.__dict__.get("__init__")
        if isinstance(init, types.FunctionType):
            def __init__(self, *args, _br_init=init, **kwargs):
                instantiated.add(type(self))
                return _br_init(self, *args, **kwargs)

            functools.update_wrapper(__init__, init)
            try:
                type.__setattr__(cls, "__init__", __init__)
            except Exception:  # noqa: BLE001
                pass
        for value in list(cls.__dict__.values()):
            if isinstance(value, type) and getattr(value, "__module__", None) == cls.__module__:
                wrap_class(value)

    def wrap_module(module):
        name = module.__name__
        for value in list(vars(module).values()):
            if isinstance(value, type):
                if getattr(value, "__module__", None) == name:
                    wrap_class(value)
            elif not isinstance(value, (types.ModuleType, types.FunctionType, types.BuiltinFunctionType)):
                cls = type(value)
                if getattr(cls, "__module__", "").startswith("sqlglot"):
                    instantiated.add(cls)

    class WrapHook(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if not (name == "sqlglot" or name.startswith("sqlglot.")):
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if spec is None or spec.loader is None or not hasattr(spec.loader, "exec_module"):
                return spec
            original = spec.loader.exec_module

            def exec_module(module, _original=original):
                _original(module)
                wrap_module(module)

            spec.loader.exec_module = exec_module
            return spec

    sys.meta_path.insert(0, WrapHook())

    sys.addaudithook(audit)
    mon.set_events(tool, mon.events.LINE | mon.events.PY_START)
    started = time.monotonic()
    status = run()
    wall = time.monotonic() - started
    mon.set_events(tool, 0)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    record = {
        "status": status,
        "wall": round(wall, 4),
        "cpu": round(usage.ru_utime + usage.ru_stime, 4),
        "func_lines": {f: sorted(v) for f, v in sorted(func_lines.items())},
        "mod_lines": {f: sorted(v) for f, v in sorted(mod_lines.items())},
        "entered": {f: sorted(v) for f, v in sorted(entered.items())},
        "opens": sorted(opens),
        "lists": sorted(lists),
        "popens": popens,
        "inst": sorted({f"{c.__module__}:{c.__qualname__}" for c in instantiated}),
    }
    with open(out_path, "w") as handle:
        json.dump(record, handle)


def run_one_test(node):
    from selector.runner import run_tests

    failed, durations = run_tests([node])
    return {"failed": node in failed, "duration": round(durations.get(node, 0.0), 4)}


def run_code(code):
    def go():
        try:
            exec(compile(code, "<string>", "exec"), {"__name__": "__main__"})
            return {"failed": False}
        except BaseException as error:  # noqa: BLE001
            return {"failed": True, "error": repr(error)}

    return go


def fork_and_trace(root, run, out_path):
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            child_trace(root, run, out_path)
        except BaseException as error:  # noqa: BLE001
            print(f"trace child crashed: {error!r}", file=sys.stderr)
            code = 1
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code)
    return pid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("repo")
    parser.add_argument("tests_json")
    parser.add_argument("out")
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--only", default=None)
    parser.add_argument("--timeout", type=float, default=900)
    options = parser.parse_args()

    root = os.path.realpath(options.repo)
    options.out = os.path.abspath(options.out)
    options.tests_json = os.path.abspath(options.tests_json)
    os.makedirs(options.out, exist_ok=True)
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    preload()

    tests = json.load(open(options.tests_json))["tests"]
    if options.only:
        tests = [t for t in tests if options.only in t["id"]]
    pending = [t for t in tests if not os.path.exists(os.path.join(options.out, f"{t['number']}.json"))]
    # Longest first keeps the pool busy at the end.
    pending.sort(key=lambda t: -t["seconds"])
    running: dict[int, tuple[dict, float]] = {}
    done = 0
    while pending or running:
        while pending and len(running) < options.jobs:
            test = pending.pop(0)
            out_path = os.path.join(options.out, f"{test['number']}.json.tmp")
            node = test["id"]
            pid = fork_and_trace(root, lambda node=node: run_one_test(node), out_path)
            running[pid] = (test, time.monotonic())
        pid, status = os.wait()
        if pid not in running:
            continue
        test, started = running.pop(pid)
        tmp = os.path.join(options.out, f"{test['number']}.json.tmp")
        final = os.path.join(options.out, f"{test['number']}.json")
        if status == 0 and os.path.exists(tmp):
            record = json.load(open(tmp))
            record["id"] = test["id"]
            # Re-run `python -c CODE` subprocesses under the tracer and merge.
            for executable, argv in record.get("popens", []):
                if len(argv) >= 3 and argv[1] == "-c" and "python" in os.path.basename(argv[0]):
                    sub_out = tmp + ".sub"
                    sub_pid = fork_and_trace(root, run_code(argv[2]), sub_out)
                    os.waitpid(sub_pid, 0)
                    if os.path.exists(sub_out):
                        sub = json.load(open(sub_out))
                        for key in ("func_lines", "mod_lines", "entered"):
                            for f, ls in sub[key].items():
                                record[key][f] = sorted(set(record[key].get(f, [])) | set(ls))
                        record["opens"] = sorted(set(record["opens"]) | set(sub["opens"]))
                        record["inst"] = sorted(set(record.get("inst", [])) | set(sub.get("inst", [])))
                        record.setdefault("subprocess_traced", []).append(argv[2])
                        os.remove(sub_out)
            json.dump(record, open(final, "w"))
            os.remove(tmp)
            if record["status"]["failed"]:
                print(f"FAILED in clean run: {test['id']}", file=sys.stderr)
        else:
            print(f"trace failed for {test['id']} (status {status})", file=sys.stderr)
        done += 1
        if done % 100 == 0:
            print(f"{done} traced, {len(pending)} pending", file=sys.stderr)


if __name__ == "__main__":
    main()
