"""pytest plugin: record, per test, which sqlglot Dialect classes and Expression
classes it instantiates. Built at the pinned commit; output JSON
{"dialects": {test: [class names]}, "exprs": {test: [class names]},
 "dialect_mro": {class: [ancestor names]}, "expr_mro": {class: [ancestor names]}}.
Enable with:  USAGE_OUT=<file> pytest -p usage_plugin
"""
import json
import os

_current = [None]
_dialects = {}
_exprs = {}


def _preimport():
    """Import every sqlglot module and dialect before any test starts, so import-time
    code runs outside test contexts and coverage files it under the empty context."""
    import importlib
    import pkgutil
    import sqlglot
    from sqlglot.dialects.dialect import Dialect, Dialects

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name == "sqlglot.__main__":
            continue
        try:
            importlib.import_module(info.name)
        except Exception:
            pass
    for name in Dialects:
        try:
            Dialect.get_or_raise(name.value)
        except Exception:
            pass


class _InlineExecutor:
    """Stand-in for ProcessPoolExecutor while building maps: run each task in this
    process, under the current test, so coverage and usage see the work."""

    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        import pickle

        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, /, *args, **kwargs):
        # Round-trip arguments and results through pickle, as a real process
        # boundary would, so serialization code is attributed to the test too.
        import concurrent.futures as cf
        import pickle

        future = cf.Future()
        try:
            args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
            future.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except BaseException as error:  # noqa: BLE001  the caller reads it from the future
            future.set_exception(error)
        return future

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return iter([self.submit(fn, *args).result() for args in zip(*iterables)])

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def pytest_configure(config):
    import concurrent.futures

    concurrent.futures.ProcessPoolExecutor = _InlineExecutor
    _preimport()
    from sqlglot.dialects import dialect as dialect_module
    from sqlglot import expressions as exp

    base_expr = getattr(exp, "Expr", None) or exp.Expression
    dialect_init = dialect_module.Dialect.__init__
    expr_init = base_expr.__init__

    def recording_dialect_init(self, *args, **kwargs):
        test = _current[0]
        if test is not None:
            _dialects.setdefault(test, set()).add(type(self).__name__)
        return dialect_init(self, *args, **kwargs)

    def recording_expr_init(self, *args, **kwargs):
        test = _current[0]
        if test is not None:
            _exprs.setdefault(test, set()).add(type(self).__name__)
        return expr_init(self, *args, **kwargs)

    dialect_module.Dialect.__init__ = recording_dialect_init
    base_expr.__init__ = recording_expr_init
    config._usage_bases = (dialect_module.Dialect, base_expr)


def _class_key(nodeid):
    parts = nodeid.split("::")
    return "CLASS:" + "::".join(parts[:2]) if len(parts) >= 3 else "FILE:" + parts[0]


def pytest_runtest_setup(item):
    # setUp / setUpClass work belongs to every test of the class
    _current[0] = _class_key(item.nodeid)


def pytest_runtest_call(item):
    _current[0] = item.nodeid


def pytest_runtest_teardown(item, nextitem):
    _current[0] = _class_key(item.nodeid)


def pytest_runtest_logfinish(nodeid, location):
    _current[0] = None


def _mro_map(base):
    seen, out, stack = set(), {}, [base]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            if sub not in seen:
                seen.add(sub)
                stack.append(sub)
    for cls in seen | {base}:
        out[cls.__name__] = [c.__name__ for c in cls.__mro__ if issubclass(c, base)]
    return out


def pytest_sessionfinish(session, exitstatus):
    dialect_base, expr_base = session.config._usage_bases
    import sqlglot.dialects  # noqa: F401  make sure every dialect class is loaded
    from sqlglot.dialects.dialect import Dialects, Dialect

    for name in Dialects:
        try:
            Dialect.get_or_raise(name.value)
        except Exception:
            pass
    out = os.environ["USAGE_OUT"]
    if os.environ.get("PYTEST_XDIST_WORKER"):
        out = f"{out}.{os.environ['PYTEST_XDIST_WORKER']}"
    with open(out, "w") as fh:
        json.dump(
            {
                "dialects": {k: sorted(v) for k, v in _dialects.items()},
                "exprs": {k: sorted(v) for k, v in _exprs.items()},
                "dialect_mro": _mro_map(dialect_base),
                "expr_mro": _mro_map(expr_base),
            },
            fh,
        )
