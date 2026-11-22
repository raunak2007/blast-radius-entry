"""pytest plugin: record, per test, which keys each class-level dict constant of sqlglot is
read with. Built at the pinned commit, only to narrow edits to one entry of such a dict.

After importing every sqlglot module and dialect, every ALL_CAPS class attribute whose
value is a plain dict is swapped for a dict subclass that notes, under the running test:
  key reads    d[k], d.get(k), k in d, d.setdefault(k), d.pop(k)
  full reads   iteration, keys/values/items, len, copy, ==, |, repr, pickling
  writes       any mutation
Work in process pools runs inline (as in usage_plugin) so it is attributed to the test.
A dict that something else still points at after the swap (an alias made at import time)
is reported, since reads through the alias would go unseen.

Output JSON (DICTKEYS_OUT, one file per xdist worker):
  {"keys": {attr: {key: [tests]}}, "full": {attr: {"sqlglot"|"tests": [tests]}},
   "writes": {attr: [tests]}, "aliased": [attr], "owners": {attr: [class qualnames]}}
Keys are canonical: "s:<str>", "e:<enum member name>", "c:<class name>", else "r:<repr>".
Enable with:  DICTKEYS_OUT=<file> pytest -p dictkeys_plugin
"""
import enum
import gc
import json
import os
import sys

_current = [None]
_keys = {}  # attr -> key -> set(tests)
_full = {}  # attr -> origin -> set(tests)
_writes = {}  # attr -> set(tests)
_owners = {}  # attr -> set(qualnames)
_aliased = set()


def canon(k):
    if isinstance(k, str):
        return "s:" + k
    if isinstance(k, enum.Enum):
        return "e:" + k.name
    if isinstance(k, type):
        return "c:" + k.__name__
    try:
        return "r:" + repr(k)
    except Exception:
        return "r:?"


def _ctx():
    """The running test, or outside tests the test file whose module-level code is reading."""
    if _current[0] is not None:
        return _current[0]
    f = sys._getframe(2)
    while f is not None:
        name = f.f_code.co_filename.replace("\\", "/")
        k = name.rfind("/tests/")
        if k >= 0 and f.f_code.co_filename != __file__:
            return "FILE:" + name[k + 1:]
        f = f.f_back
    return "<no test>"


def _caller_origin(depth=2):
    f = sys._getframe(depth)
    while f is not None and f.f_code.co_filename == __file__:
        f = f.f_back
    name = f.f_code.co_filename if f is not None else ""
    return "tests" if "/tests/" in name.replace("\\", "/") else "sqlglot"


class KeyTrackDict(dict):
    __slots__ = ("_br_attr",)

    def _key(self, k):
        _keys.setdefault(self._br_attr, {}).setdefault(canon(k), set()).add(_ctx())

    def _all(self):
        _full.setdefault(self._br_attr, {}).setdefault(_caller_origin(3), set()).add(_ctx())

    def _write(self):
        _writes.setdefault(self._br_attr, set()).add(_ctx())

    # key reads
    def __getitem__(self, k):
        self._key(k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        self._key(k)
        return dict.get(self, k, default)

    def __contains__(self, k):
        self._key(k)
        return dict.__contains__(self, k)

    # full reads
    def __iter__(self):
        self._all()
        return dict.__iter__(self)

    def keys(self):
        self._all()
        return dict.keys(self)

    def values(self):
        self._all()
        return dict.values(self)

    def items(self):
        self._all()
        return dict.items(self)

    def __len__(self):
        self._all()
        return dict.__len__(self)

    def copy(self):
        self._all()
        return dict(dict.items(self))

    def __eq__(self, other):
        self._all()
        return dict.__eq__(self, other)

    def __ne__(self, other):
        self._all()
        return dict.__ne__(self, other)

    __hash__ = None

    def __or__(self, other):
        self._all()
        return dict.__or__(dict(dict.items(self)), other)

    def __ror__(self, other):
        self._all()
        return dict.__or__(dict(other), dict(dict.items(self)))

    def __reversed__(self):
        self._all()
        return dict.__reversed__(self)

    def __repr__(self):
        self._all()
        return dict.__repr__(self)

    def __reduce_ex__(self, protocol):
        self._all()
        return (dict, (dict(dict.items(self)),))

    # writes
    def __setitem__(self, k, v):
        self._write()
        dict.__setitem__(self, k, v)

    def __delitem__(self, k):
        self._write()
        dict.__delitem__(self, k)

    def setdefault(self, k, default=None):
        self._key(k)
        if not dict.__contains__(self, k):
            self._write()
        return dict.setdefault(self, k, default)

    def pop(self, k, *default):
        self._key(k)
        self._write()
        return dict.pop(self, k, *default)

    def popitem(self):
        self._write()
        return dict.popitem(self)

    def update(self, *args, **kwargs):
        self._write()
        dict.update(self, *args, **kwargs)

    def clear(self):
        self._write()
        dict.clear(self)

    def __ior__(self, other):
        self._write()
        dict.update(self, other)
        return self


def _sqlglot_classes():
    import inspect

    seen, out = set(), []
    for name, mod in list(sys.modules.items()):
        if not (name == "sqlglot" or name.startswith("sqlglot.")) or mod is None:
            continue
        for obj in list(vars(mod).values()):
            if not inspect.isclass(obj):
                continue
            stack = [obj]
            while stack:
                cls = stack.pop()
                if id(cls) in seen or not getattr(cls, "__module__", "").startswith("sqlglot"):
                    continue
                seen.add(id(cls))
                out.append(cls)
                stack.extend(v for v in vars(cls).values() if inspect.isclass(v))
                try:
                    stack.extend(cls.__subclasses__())
                except TypeError:
                    pass
    return out


def _instrument():
    originals = {}
    for cls in _sqlglot_classes():
        for attr, value in list(vars(cls).items()):
            if type(value) is not dict or not attr.isupper() or attr.startswith("__"):
                continue
            tracked = KeyTrackDict(value)
            tracked._br_attr = attr
            try:
                setattr(cls, attr, tracked)
            except (TypeError, AttributeError):
                continue
            originals.setdefault(id(value), (value, set()))[1].add(attr)
            _owners.setdefault(attr, set()).add(f"{cls.__module__}.{cls.__qualname__}")
    value = tracked = None  # drop this frame's references before looking for aliases
    # anything still holding one of the original dicts reads it unseen
    ids = set(originals)
    keep = {id(originals), id(ids)}
    for obj in gc.get_objects():
        if id(obj) in keep or obj is originals:
            continue
        for ref in gc.get_referents(obj):
            if id(ref) in ids and type(ref) is dict:
                if isinstance(obj, tuple) and len(obj) == 2 and obj[0] is ref and isinstance(obj[1], set):
                    continue  # our own bookkeeping tuple
                _aliased.update(originals[id(ref)][1])
    originals.clear()


def pytest_configure(config):
    import concurrent.futures

    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from usage_plugin import _InlineExecutor

    concurrent.futures.ProcessPoolExecutor = _InlineExecutor
    _import_all()
    _instrument()  # before any sqlglot code runs, so no runtime cache is built from an untracked dict


def _import_all():
    """Import every sqlglot module (so every class exists), without running anything else."""
    import importlib
    import pkgutil
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name == "sqlglot.__main__":
            continue
        try:
            importlib.import_module(info.name)
        except Exception:
            pass


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
    out = os.environ["DICTKEYS_OUT"]
    if os.environ.get("PYTEST_XDIST_WORKER"):
        out = f"{out}.{os.environ['PYTEST_XDIST_WORKER']}"
    with open(out, "w") as fh:
        json.dump(
            {
                "keys": {a: {k: sorted(v) for k, v in d.items()} for a, d in _keys.items()},
                "full": {a: {o: sorted(v) for o, v in d.items()} for a, d in _full.items()},
                "writes": {a: sorted(v) for a, v in _writes.items()},
                "aliased": sorted(_aliased),
                "owners": {a: sorted(v) for a, v in _owners.items()},
            },
            fh,
        )
