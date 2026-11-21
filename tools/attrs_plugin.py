"""pytest plugin: record, per test, which class-level constants of sqlglot's classes it read,
by the class that holds them, at the pinned commit.

After collection (usage_plugin has imported every module by then), every UPPER_CASE class
attribute of every sqlglot class whose value is a bool, int, float, str or None is replaced
by a descriptor that returns the same value and records (module, class, name) under the
current context: the test's node id while it runs, its class during setup and teardown,
IMPORT outside tests. A read through a subclass that doesn't override the name resolves to
the class that defines it, so an edit there reaches exactly these reads. Reads while modules
were imported happen before the wrapping; the selector uses this data only for names no
import-time code reads, or after its import-time state check.

Output: pickle {"wrapped": [(rel, qualname, name)], "reads": {(rel, qualname, name): set(ctx)}}
Enable with: ATTRS_ROOT=<repo> ATTRS_OUT=<file> pytest -p usage_plugin -p attrs_plugin
"""
import os
import pickle
import sys

ROOT = os.path.realpath(os.environ["ATTRS_ROOT"])
PKG = os.path.join(ROOT, "sqlglot") + os.sep
OUT = os.environ["ATTRS_OUT"]
PRIMS = (bool, int, float, str, type(None))

_current = ["IMPORT"]
_wrapped = []
_reads = {}


class _Rec:
    __slots__ = ("value", "key")

    def __init__(self, value, key):
        self.value = value
        self.key = key

    def __get__(self, obj, owner=None):
        ctxs = _reads.get(self.key)
        if ctxs is None:
            ctxs = _reads[self.key] = set()
        ctxs.add(_current[0])
        return self.value


def _wrap_all():
    seen = set()

    def visit(cls):
        if id(cls) in seen:
            return
        seen.add(id(cls))
        mod = sys.modules.get(cls.__module__)
        path = os.path.realpath(getattr(mod, "__file__", "") or "")
        if not path.startswith(PKG) or "<locals>" in cls.__qualname__:
            return
        rel = os.path.relpath(path, ROOT)
        for name, value in list(vars(cls).items()):
            if isinstance(value, type):
                visit(value)
                continue
            if not name.isupper() or name.startswith("_") or type(value) not in PRIMS:
                continue
            key = (rel, cls.__qualname__, name)
            try:
                setattr(cls, name, _Rec(value, key))
            except (AttributeError, TypeError):
                continue
            _wrapped.append(key)

    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if not f or not os.path.realpath(f).startswith(PKG):
            continue
        for value in list(vars(mod).values()):
            if isinstance(value, type) and getattr(value, "__module__", None) == name:
                visit(value)


def _class_key(nodeid):
    parts = nodeid.split("::")
    return "CLASS:" + "::".join(parts[:2]) if len(parts) >= 3 else "FILE:" + parts[0]


def pytest_collection_finish(session):
    _wrap_all()


def pytest_runtest_setup(item):
    _current[0] = _class_key(item.nodeid)


def pytest_runtest_call(item):
    _current[0] = item.nodeid


def pytest_runtest_teardown(item, nextitem):
    _current[0] = _class_key(item.nodeid)


def pytest_runtest_logfinish(nodeid, location):
    _current[0] = "IMPORT"


def pytest_sessionfinish(session, exitstatus):
    out = OUT
    if os.environ.get("PYTEST_XDIST_WORKER"):
        out = f"{OUT}.{os.environ['PYTEST_XDIST_WORKER']}"
    with open(out, "wb") as fh:
        pickle.dump({"wrapped": _wrapped, "reads": _reads}, fh, protocol=4)
