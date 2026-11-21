"""pytest plugin: record, per test, which defaulted parameters of sqlglot's module- and
class-level functions some call left at their default, at the pinned commit.

A changed default value can only reach a call that leaves the parameter out. Every such
function's code object gets a sys.monitoring PY_START callback that compares each
defaulted parameter's bound value with the function's default object (an explicit argument
that happens to be the very same object counts as left out, which only keeps more tests).
Contexts as in the other plugins: the test's node id while it runs, its class during
setup and teardown, IMPORT outside tests. After CAP calls of one function in one context
the callback stops looking and counts every default of that function as used there.

Functions are found by walking every imported sqlglot module and the classes defined in
it (nested classes too), unwrapping decorators. A code object shared by function objects
with different default objects is left out (the selector keeps its usual selection).

Output: pickle {"funcs": {(rel, firstlineno, name): (param names)}, "records": {(rel,
firstlineno, name): {ctx: set(param names)}}}
Enable with: DEFAULTS_ROOT=<repo> DEFAULTS_OUT=<file> pytest -p usage_plugin -p defaults_plugin
(usage_plugin first: it inlines process pools and pre-imports every module).
"""
import os
import pickle
import sys
import types

ROOT = os.path.realpath(os.environ["DEFAULTS_ROOT"])
PKG = os.path.join(ROOT, "sqlglot") + os.sep
OUT = os.environ["DEFAULTS_OUT"]
CAP = 200
mon = sys.monitoring
TOOL = 4

_current = ["IMPORT"]
_spec = {}  # code -> (key, ((name, default object), ...))
_funcs = {}  # key -> param names
_records = {}  # key -> {ctx: set(names)}
_counts = {}  # code -> calls in the current context
_done = set()  # codes finished for the current context


def _unwrap(f):
    seen = set()
    while True:
        if isinstance(f, (staticmethod, classmethod)):
            f = f.__func__
        inner = getattr(f, "__wrapped__", None)
        if inner is None or id(inner) in seen:
            return f
        seen.add(id(f))
        f = inner


def _defaults(f):
    code = f.__code__
    out = []
    pos = code.co_varnames[: code.co_argcount]
    for name, d in zip(pos[len(pos) - len(f.__defaults__ or ()):], f.__defaults__ or ()):
        out.append((name, d))
    for name, d in (f.__kwdefaults__ or {}).items():
        out.append((name, d))
    return tuple(out)


def _collect():
    found = {}  # code -> list of (key, defaults)
    bad = set()

    def add(f):
        f = _unwrap(f)
        if not isinstance(f, types.FunctionType):
            return
        code = f.__code__
        path = os.path.realpath(code.co_filename)
        if not path.startswith(PKG):
            return
        defs = _defaults(f)
        if not defs:
            return
        key = (os.path.relpath(path, ROOT), code.co_firstlineno, code.co_name)
        found.setdefault(code, []).append((key, defs))

    def walk_class(cls, seen):
        if id(cls) in seen:
            return
        seen.add(id(cls))
        for value in list(vars(cls).values()):
            if isinstance(value, property):
                for g in (value.fget, value.fset, value.fdel):
                    if g is not None:
                        add(g)
            elif isinstance(value, type):
                walk_class(value, seen)
            else:
                add(value)

    seen = set()
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if not f or not os.path.realpath(f).startswith(PKG):
            continue
        for value in list(vars(mod).values()):
            if isinstance(value, type):
                walk_class(value, seen)
            else:
                add(value)
    for code, entries in found.items():
        first_key, first_defs = entries[0]
        same = all(k == first_key and len(d) == len(first_defs) and all(a[0] == b[0] and a[1] is b[1]
                                                                        for a, b in zip(d, first_defs))
                   for k, d in entries)
        if not same:
            bad.add(first_key)
            continue
        _spec[code] = (first_key, first_defs)
        _funcs[first_key] = tuple(n for n, _ in first_defs)
    for code, (key, _) in list(_spec.items()):
        if key in bad:
            del _spec[code]
            _funcs.pop(key, None)


def _on_start(code, offset):
    spec = _spec.get(code)
    if spec is None or code in _done:
        return mon.DISABLE
    key, defs = spec
    ctx = _current[0]
    per = _records.setdefault(key, {})
    used = per.setdefault(ctx, set())
    n = _counts.get(code, 0) + 1
    _counts[code] = n
    if n > CAP:
        used.update(name for name, _ in defs)  # stop looking: count every default as used
        _done.add(code)
        return mon.DISABLE
    loc = sys._getframe(1).f_locals
    for name, d in defs:
        if name not in used:
            try:
                if loc[name] is d:
                    used.add(name)
            except KeyError:
                used.add(name)
    if len(used) == len(defs):
        _done.add(code)  # nothing more to learn in this context
        return mon.DISABLE
    return None


def _switch(ctx):
    _current[0] = ctx
    _counts.clear()
    _done.clear()
    mon.restart_events()


def _class_key(nodeid):
    parts = nodeid.split("::")
    return "CLASS:" + "::".join(parts[:2]) if len(parts) >= 3 else "FILE:" + parts[0]


def pytest_collection_finish(session):
    _collect()
    mon.use_tool_id(TOOL, "defaults_plugin")
    mon.register_callback(TOOL, mon.events.PY_START, _on_start)
    for code in _spec:
        mon.set_local_events(TOOL, code, mon.events.PY_START)


def pytest_runtest_setup(item):
    _switch(_class_key(item.nodeid))


def pytest_runtest_call(item):
    _switch(item.nodeid)


def pytest_runtest_teardown(item, nextitem):
    _switch(_class_key(item.nodeid))


def pytest_runtest_logfinish(nodeid, location):
    _switch("IMPORT")


def pytest_sessionfinish(session, exitstatus):
    out = OUT
    if os.environ.get("PYTEST_XDIST_WORKER"):
        out = f"{OUT}.{os.environ['PYTEST_XDIST_WORKER']}"
    with open(out, "wb") as fh:
        pickle.dump({"funcs": _funcs, "records": _records}, fh, protocol=4)
