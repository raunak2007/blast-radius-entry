"""Check whether a patch makes sqlglot crash while importing.

A class-body or module-level edit can raise at import time (for example a subclass
that pops a key the edit renamed). Every test in every file that imports the module
then fails.

One checker process imports sqlglot once. For each check it forks a child that serves
the patched files from memory over the pristine tree and imports sqlglot (from scratch
only if a patched module was already loaded), every dialect and the given modules.
Leaf modules such as a dialect file cost a few tens of milliseconds; core modules
cost one full `import sqlglot`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

BR_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHECKER = r"""
import importlib, importlib.abc, importlib.util, json, os, sys
root = sys.argv[1]
sys.path.insert(0, root)
import sqlglot  # warm: shared by every forked check
from sqlglot.dialects.dialect import DIALECT_MODULE_NAMES
WARM = json.loads(sys.argv[2])
sys.path.append(sys.argv[3])
from br import statefp
PRISTINE = None  # snapshot of the warm, unpatched state, taken once before the first fork
for _name in WARM + ["sqlglot.dialects." + k for k in DIALECT_MODULE_NAMES]:
    try:
        importlib.import_module(_name)
    except Exception:
        pass

class Loader(importlib.abc.Loader):
    def __init__(self, path, src):
        self.path, self.src = path, src
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        module.__file__ = os.path.join(root, self.path)
        exec(compile(self.src, module.__file__, "exec"), module.__dict__)

def finder_for(files):
    patched = {}
    for p, src in files.items():
        name = p[:-3].replace("/", ".")
        patched[name[: -len(".__init__")] if name.endswith(".__init__") else name] = (p, src)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name not in patched:
                return None
            rel, src = patched[name]
            is_pkg = rel.endswith("__init__.py")
            spec = importlib.util.spec_from_loader(
                name, Loader(rel, src), origin=os.path.join(root, rel), is_package=is_pkg)
            if is_pkg:
                spec.submodule_search_locations = [os.path.dirname(os.path.join(root, rel))]
            return spec

    return patched, Finder()


class InlineExecutor:
    # Process pools would import the unpatched files from disk: run their work here,
    # with the same pickle round trips a spawned worker would do.
    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*roundtrip(initargs))
    def submit(self, fn, *args, **kwargs):
        import concurrent.futures
        future = concurrent.futures.Future()
        try:
            args, kwargs = roundtrip((args, kwargs))
            future.set_result(roundtrip(fn(*args, **kwargs)))
        except BaseException as error:
            future.set_exception(error)
        return future
    def map(self, fn, *iterables, **kwargs):
        return [self.submit(fn, *args).result() for args in zip(*iterables)]
    def shutdown(self, *args, **kwargs):
        return None
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False

def roundtrip(value):
    import pickle
    return pickle.loads(pickle.dumps(value))

def no_subprocess(*args, **kwargs):
    raise RuntimeError("tests that start processes cannot be checked here")

def run_tests(request):
    # A fresh import of everything, as in a new pytest worker: nothing the warm parent
    # built from unpatched code (instance caches, registries) can leak into the run.
    import concurrent.futures, multiprocessing, signal, subprocess, unittest
    signal.alarm(max(1, int(request["timeout"])))
    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    subprocess.Popen.__init__ = no_subprocess
    multiprocessing.Process.start = no_subprocess
    os.fork = no_subprocess
    patched, finder = finder_for(request["files"])
    for name in list(sys.modules):
        if name in ("sqlglot", "tests") or name.startswith(("sqlglot.", "tests.")):
            del sys.modules[name]
    sys.meta_path.insert(0, finder)
    os.chdir(root)
    saved = os.dup(1)
    quiet = os.open(os.devnull, os.O_WRONLY)
    os.dup2(quiet, 1)
    sys.stdout = open(os.devnull, "w")

    class Result(unittest.TestResult):
        failed = set()
        def _mark(self, test):
            self.failed.add(getattr(test, "id", lambda: "<setup>")())
        def addError(self, test, err):
            self._mark(test)
        def addFailure(self, test, err):
            self._mark(test)
        def addUnexpectedSuccess(self, test):
            self._mark(test)
        def addSubTest(self, test, subtest, err):
            if err is not None:
                self._mark(test)

    by_unittest_id = {}
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for tid in request["tests"]:
        path, klass, method = tid.split("::")
        dotted = path[:-3].replace("/", ".") + "." + klass + "." + method
        by_unittest_id[dotted] = tid
        suite.addTest(loader.loadTestsFromName(dotted))
    result = Result()
    suite.run(result)
    failed = sorted(by_unittest_id.get(t, "<other>") for t in result.failed)
    os.write(saved, ("ran " + json.dumps(failed) + "\n").encode())


def run(request):
    if "tests" in request:
        return run_tests(request)
    patched, finder = finder_for(request["files"])
    purge = set(patched) | set(request["dependents"])
    core = request["core"]
    for name in [n for n in sys.modules if n == "sqlglot" or n.startswith("sqlglot.")]:
        if core or name in purge:
            del sys.modules[name]
    sys.meta_path.insert(0, finder)
    importlib.import_module("sqlglot")
    names = sorted(purge) if not core else WARM + list(patched) + ["sqlglot.dialects." + k for k in DIALECT_MODULE_NAMES]
    for name in names:
        if not name.startswith("tests"):
            importlib.import_module(name)
    if request.get("state"):
        after = statefp.snapshot()
        changed = {k: statefp.kind_of(PRISTINE, after, k) for k in statefp.diff(PRISTINE[0], after[0])}
        print("state " + json.dumps(changed), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    if request.get("state") and PRISTINE is None:
        PRISTINE = statefp.snapshot()
    pid = os.fork()
    if pid == 0:
        try:
            run(request)
            code = 0
        except BaseException:
            code = 1
        sys.stdout.flush()
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    print("crash" if os.waitstatus_to_exitcode(status) != 0 else "ok", flush=True)
"""


class ImportChecker:
    """Lazily started checker process; fails safe (any error counts as a crash)."""

    def __init__(self, src_root: str, warm_modules: list[str], dependents: dict[str, list[str]]) -> None:
        self.src_root = src_root
        self.warm = warm_modules
        self.dependents = dependents  # module path -> module names whose import loads it
        self.proc: subprocess.Popen | None = None

    def _start(self) -> subprocess.Popen:
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(
                [sys.executable, "-c", CHECKER, self.src_root, json.dumps(self.warm), BR_PARENT], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"},
            )
        return self.proc

    def crashes(self, patched: dict[str, str]) -> bool:
        return self.check(patched, state=False)[0]

    def check(self, patched: dict[str, str], state: bool = True) -> tuple[bool, dict[str, str] | None]:
        """(crashed, changed) for the patched sources.

        `changed` maps every import-time state key ("module::Qual.attr") whose value
        differs from the unpatched import to "data", "callable" or "class"; None when
        unknown. Core modules (loaded by `import sqlglot`, so no dependents entry)
        re-import everything.
        """
        deps: set[str] = set()
        core = False
        for path in patched:
            if path not in self.dependents:
                core = True
            deps.update(self.dependents.get(path, ()))
        request = {"files": patched, "dependents": sorted(deps), "core": core, "state": state}
        status, extras = self._send(request)
        if status is None:
            return True, None
        return status != "ok", extras.get("state")

    def run_tests(self, patched: dict[str, str], test_ids: list[str], timeout: float) -> set[str] | None:
        """Ids of the given tests that fail on the patched sources, run in a fresh import
        of everything. None when the run did not finish (crash, timeout, protocol)."""
        status, extras = self._send({"files": patched, "tests": test_ids, "timeout": timeout})
        if status != "ok" or "ran" not in extras:
            return None
        return set(extras["ran"])

    def _send(self, request: dict) -> tuple[str | None, dict]:
        extras: dict = {}
        try:
            proc = self._start()
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            while True:
                line = proc.stdout.readline()
                if not line:
                    raise OSError("checker exited")
                for tag in ("state", "ran"):
                    if line.startswith(tag + " "):
                        extras[tag] = json.loads(line[len(tag) + 1:])
                        break
                else:
                    return line.strip(), extras
        except (OSError, ValueError):
            self.proc = None
            return None, {}

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
