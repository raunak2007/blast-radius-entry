"""pytest plugin: dictkeys_plugin's records, also kept per dict (the class that holds it), with
which class-body dicts were built from which (build_dictkeys2.py). Built at the pinned commit,
only to narrow edits to one entry of one class's dict to the lookups in that dict and in the
dicts its class body copies were built into.

Everything below is as in dictkeys_plugin, plus:
  owned        {attr: {owner: {key: [tests]}}}  owner = "<module>.<qualname>" of the class
               whose own __dict__ holds the dict (a subclass that doesn't override it reads
               the same object, so it counts under the parent)
  derived      {attr: {owner: [source owners] | None}}: the class-body assignment of attr
               (the only one in that body) and the `X.attr` it reads, with X evaluated in
               the module (and enclosing class) namespace and resolved along X's MRO. None
               when that can't be told: no such single assignment, a bare `attr` name in it,
               an `X.attr` that doesn't evaluate, or a top-level call building the value.

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
Enable with:  DICTKEYS_OUT=<file> pytest -p dictkeys2_plugin
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
_owned = {}  # attr -> owner -> key -> set(tests)
_derived = {}  # attr -> owner -> set(owners) | None


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
    __slots__ = ("_br_attr", "_br_owner")

    def _key(self, k):
        ctx = _ctx()
        c = canon(k)
        _keys.setdefault(self._br_attr, {}).setdefault(c, set()).add(ctx)
        _owned.setdefault(self._br_attr, {}).setdefault(self._br_owner, {}).setdefault(c, set()).add(ctx)

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


_trees = {}


def _class_body(cls):
    """The ClassDef of `cls` in its module's source, or None."""
    import ast

    mod = sys.modules.get(cls.__module__)
    path = getattr(mod, "__file__", None)
    if not path or "<" in cls.__qualname__:
        return None, None
    if path not in _trees:
        with open(path, encoding="utf-8") as fh:
            _trees[path] = ast.parse(fh.read())
    body, chain = _trees[path].body, []
    node = None
    for part in cls.__qualname__.split("."):
        found = [n for n in body if isinstance(n, ast.ClassDef) and n.name == part]
        if len(found) != 1:
            return None, None
        node = found[0]
        chain.append(node)
        body = node.body
    return node, chain


def _owner_of(klass, attr):
    for c in getattr(klass, "__mro__", ()):
        if attr in vars(c):
            return f"{c.__module__}.{c.__qualname__}"
    return None


def _sources(cls, attr):
    """The owners of the `X.attr` dicts the class body's `attr = ...` reads (see the module doc)."""
    import ast

    node, chain = _class_body(cls)
    if node is None:
        return None
    assigns = []

    def walk(stmts):
        for s in stmts:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == attr for t in s.targets):
                assigns.append(s)
            elif isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name) and s.target.id == attr:
                assigns.append(s)
            elif isinstance(s, ast.AugAssign) and isinstance(s.target, ast.Name) and s.target.id == attr:
                assigns.append(s)
            for field in ("body", "orelse", "finalbody", "handlers"):
                walk(getattr(s, field, None) or [])

    walk(node.body)
    if len(assigns) != 1 or not isinstance(assigns[0], (ast.Assign, ast.AnnAssign)) or assigns[0].value is None:
        return None
    if isinstance(assigns[0], ast.Assign) and len(assigns[0].targets) != 1:
        return None
    value = assigns[0].value
    if isinstance(value, ast.Call):
        # X.attr.copy() and dict(X.attr) copy it; any other call is a builder whose reads
        # can't be told from here
        f = value.func
        copy = isinstance(f, ast.Attribute) and f.attr == "copy" and not value.args and not value.keywords \
            and isinstance(f.value, ast.Attribute) and f.value.attr == attr
        plain = isinstance(f, ast.Name) and f.id == "dict" and len(value.args) == 1 and not value.keywords \
            and isinstance(value.args[0], ast.Attribute) and value.args[0].attr == attr
        if not (copy or plain):
            return None
    mod = sys.modules[cls.__module__]
    namespace = {}
    # enclosing classes' namespaces (a nested class body can't see them, but sources
    # written as Outer.Inner.attr resolve through the module anyway)
    out = set()
    for n in ast.walk(value):
        if isinstance(n, ast.Lambda):
            continue
        if isinstance(n, ast.Name) and n.id == attr:
            return None
        if isinstance(n, ast.Attribute) and n.attr == attr:
            try:
                base = eval(compile(ast.Expression(n.value), "<src>", "eval"), dict(vars(mod)), namespace)
            except Exception:
                return None
            owner = _owner_of(base, attr)
            if owner is None:
                return None
            out.add(owner)
    return out


def _instrument():
    originals = {}
    for cls in _sqlglot_classes():
        for attr, value in list(vars(cls).items()):
            if type(value) is not dict or not attr.isupper() or attr.startswith("__"):
                continue
            tracked = KeyTrackDict(value)
            tracked._br_attr = attr
            owner = f"{cls.__module__}.{cls.__qualname__}"
            tracked._br_owner = owner
            try:
                srcs = _sources(cls, attr)
            except Exception:
                srcs = None
            per = _derived.setdefault(attr, {})
            if owner in per and (per[owner] is None or srcs is None):
                per[owner] = None
            elif owner in per:
                per[owner] = per[owner] | srcs
            else:
                per[owner] = srcs
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
                "owned": {a: {o: {k: sorted(v) for k, v in per.items()} for o, per in d.items()}
                          for a, d in _owned.items()},
                "derived": {a: {o: (sorted(s) if s is not None else None) for o, s in d.items()}
                            for a, d in _derived.items()},
            },
            fh,
        )
