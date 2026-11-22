"""Pytest plugin: record which files under tests/fixtures each test opens.

Used only offline to build maps/fixtures.json from the pinned sqlglot commit.
Run with: pytest -p fixture_tracer (tools/ on PYTHONPATH). Output path comes
from the FIXTURE_TRACE_OUT environment variable.
"""

import builtins
import concurrent.futures
import io
import json
import os
import pickle
import sys

import pytest

_real_open = builtins.open
_real_io_open = io.open
_current: list[str] = []
_seen: dict[str, set[str]] = {}


def _record(path) -> None:
    if not _current:
        return
    try:
        resolved = os.path.realpath(os.fspath(path))
    except TypeError:
        return
    marker = os.sep + "tests" + os.sep + "fixtures" + os.sep
    if marker in resolved:
        rel = "tests/fixtures/" + resolved.split(marker, 1)[1].replace(os.sep, "/")
        _seen.setdefault(_current[0], set()).add(rel)


def _traced_open(file, *args, **kwargs):
    _record(file)
    return _real_open(file, *args, **kwargs)


def _roundtrip(value):
    """Pickle and unpickle, as a real process pool would (sqlglot pickles via serde)."""
    return pickle.loads(pickle.dumps(value))


class InlineExecutor:
    """Drop-in ProcessPoolExecutor that runs work in-process so coverage sees it."""

    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*_roundtrip(initargs))

    def submit(self, fn, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            args, kwargs = _roundtrip((args, kwargs))
            future.set_result(_roundtrip(fn(*args, **kwargs)))
        except BaseException as error:  # noqa: BLE001 - mirror executor semantics
            future.set_exception(error)
        return future

    def shutdown(self, *args, **kwargs):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_classes: dict[str, set[str]] = {}
_key_cache: dict[type, tuple[str, ...]] = {}


def _module_path(module_name: str) -> str | None:
    module = sys.modules.get(module_name)
    path = getattr(module, "__file__", None)
    if not path or not module_name.startswith("sqlglot"):
        return None
    base = module_name.replace(".", "/")
    return f"{base}/__init__.py" if path.endswith("__init__.py") else f"{base}.py"


def _class_keys(cls) -> tuple[str, ...]:
    """'sqlglot/<file>.py::<Qualname>' for every sqlglot class in cls's MRO."""
    cached = _key_cache.get(cls)
    if cached is None:
        keys = []
        for klass in cls.__mro__:
            path = _module_path(getattr(klass, "__module__", "") or "")
            if path:
                keys.append(f"{path}::{klass.__qualname__}")
        cached = _key_cache[cls] = tuple(keys)
    return cached


def _wrap_init(klass) -> None:
    original = klass.__dict__.get("__init__")
    if original is None or getattr(original, "_br_wrapped", False):
        return

    def __init__(self, *args, **kwargs):
        if _current:
            _classes.setdefault(_current[0], set()).update(_class_keys(type(self)))
        return original(self, *args, **kwargs)

    __init__._br_wrapped = True
    try:
        klass.__init__ = __init__
    except (TypeError, AttributeError):
        pass


def _record_class_usage() -> None:
    """Record which sqlglot classes each test constructs, so class-level edits reach them.

    Imports every sqlglot module first, then wraps every __init__ a sqlglot class
    defines, so lazily imported modules (lineage, optimizer, executor, dialects) and
    their own constructors are traced too.
    """
    import importlib
    import inspect
    import pkgutil

    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except Exception:  # noqa: BLE001 - optional extras (e.g. mypyc cores) may be missing
            pass

    for name, module in list(sys.modules.items()):
        if not name.startswith("sqlglot") or module is None:
            continue
        for _, klass in inspect.getmembers(module, inspect.isclass):
            if getattr(klass, "__module__", "") == name:
                _wrap_init(klass)


def pytest_configure(config):
    if os.environ.get("INLINE_POOLS"):
        concurrent.futures.ProcessPoolExecutor = InlineExecutor
    builtins.open = _traced_open
    io.open = _traced_open
    if os.environ.get("TRACE_CLASSES"):
        _record_class_usage()


def pytest_unconfigure(config):
    builtins.open = _real_open
    io.open = _real_io_open


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    """Files a test module opens while importing belong to every test in that module."""
    if isinstance(collector, pytest.Module):
        _current[:] = [f"collect:{collector.nodeid}"]
        try:
            yield
        finally:
            _current.clear()
    else:
        yield


def _phase(item, phase: str):
    _current[:] = [f"{item.nodeid}|{phase}"]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _phase(item, "setup")
    yield
    _current.clear()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _phase(item, "run")
    yield
    _current.clear()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _phase(item, "teardown")
    yield
    _current.clear()


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("FIXTURE_TRACE_OUT")
    if not out:
        return
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with _real_open(f"{out}.{worker}.json", "w", encoding="utf-8") as handle:
        json.dump({k: sorted(v) for k, v in _seen.items()}, handle, indent=0, sort_keys=True)
    with _real_open(f"{out}-classes.{worker}.json", "w", encoding="utf-8") as handle:
        json.dump({k: sorted(v) for k, v in _classes.items()}, handle, indent=0, sort_keys=True)
