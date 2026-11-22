"""pytest plugin: which (expression class, arg) pairs are checked while missing, per test.

An expression class's `arg_types` values (True = required) are only read once, at class
creation, to build `required_args`, which only Expression.error_messages reads. So flipping
one entry's True/False can only change what error_messages reports for that class (or a
class that inherits or copies its arg_types) when that arg is missing. This records, for
every error_messages call, the class that owns the arg_types in use and which of its keys
were missing then. Calls outside any test (import time, collection) are kept under None.

The wrapper is installed the moment sqlglot.expressions.core has run, before anything can
parse. Work in process pools runs inline, as in usage_plugin.

Output (ARGREQ_OUT, one file per xdist worker), JSON:
  {"missing": {"<module>.<qualname>|<key>": [ctx or "<outside>"]}}
Enable with:  ARGREQ_OUT=<file> pytest -p argreq_plugin
"""
import importlib.abc
import importlib.machinery
import json
import os
import sys

_current = [None]
_missing = {}  # (owner, key) -> set(ctx)


def _owner_of(cls):
    for k in cls.__mro__:
        if "arg_types" in vars(k):
            return k
    return None


def _patch(core):
    original = core.Expression.error_messages

    def error_messages(self, args=None):
        owner = _owner_of(type(self))
        if owner is not None:
            name = f"{owner.__module__}.{owner.__qualname__}"
            ctx = _current[0]
            for k in owner.arg_types:
                v = self.args.get(k)
                if v is None or (isinstance(v, list) and not v):
                    _missing.setdefault((name, k), set()).add(ctx)
        return original(self, args)

    core.Expression.error_messages = error_messages


class _Loader(importlib.abc.Loader):
    def __init__(self, inner):
        self.inner = inner

    def create_module(self, spec):
        return self.inner.create_module(spec)

    def exec_module(self, module):
        self.inner.exec_module(module)
        _patch(module)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "sqlglot.expressions.core":
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is not None and spec.loader is not None:
            spec.loader = _Loader(spec.loader)
        return spec


def pytest_configure(config):
    assert "sqlglot" not in sys.modules, "argreq_plugin must load before sqlglot"
    import concurrent.futures

    sys.meta_path.insert(0, _Finder())
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from usage_plugin import _InlineExecutor

    concurrent.futures.ProcessPoolExecutor = _InlineExecutor


def _class_key(nodeid):
    parts = nodeid.split("::")
    return "CLASS:" + "::".join(parts[:2]) if len(parts) >= 3 else "FILE:" + parts[0]


def pytest_runtest_setup(item):
    _current[0] = _class_key(item.nodeid)


def pytest_runtest_call(item):
    _current[0] = item.nodeid


def pytest_runtest_teardown(item, nextitem):
    _current[0] = _class_key(item.nodeid)


def pytest_runtest_logfinish(nodeid, location):
    _current[0] = None


def pytest_sessionfinish(session, exitstatus):
    out = os.environ["ARGREQ_OUT"]
    if os.environ.get("PYTEST_XDIST_WORKER"):
        out = f"{out}.{os.environ['PYTEST_XDIST_WORKER']}"
    with open(out, "w") as fh:
        json.dump({"missing": {f"{o}|{k}": sorted("<outside>" if c is None else c for c in v)
                               for (o, k), v in _missing.items()}}, fh)
