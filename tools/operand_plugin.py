"""pytest plugin: record, per test, the operands every single-operator comparison in
sqlglot/ sees, at the pinned commit. The selector re-evaluates an edited comparison
(an operator swap or a changed literal) on these inputs and drops the tests whose
result can't change. (Idea credited to the skbgp entry; this is our own implementation.)

Every `a OP b` in sqlglot/ is compiled as `_br_cmp_(site, a, b)`, which records an
abstract form of both operands and returns `a OP b`. Comparisons run while no test is
running are filed under IMPORT; setup/teardown under the test's class, like the
other plugins. More than CAP distinct operand pairs at one site in one context, or
an operand we can't describe, marks that test as unknown there (the selector keeps it).

Output: pickle {"sites": {sid: (rel, line, col, op)}, "records": {sid: {ctx: set | None}},
                "enums": {cls: {"members": {name: value_rep}, "identity": bool}}}
Enable with:  OPERAND_ROOT=<repo> OPERAND_OUT=<file> pytest -p usage_plugin -p operand_plugin
(usage_plugin first: it inlines process pools and pre-imports every module.)
"""
import ast
import builtins
import enum
import importlib.abc
import importlib.machinery
import operator
import os
import pickle
import sys

ROOT = os.path.realpath(os.environ["OPERAND_ROOT"])
PKG = os.path.join(ROOT, "sqlglot") + os.sep
OUT = os.environ["OPERAND_OUT"]
CAP = 24
MAX_STR = 200
MAX_ITEMS = 32

OPS = {
    ast.Eq: ("==", operator.eq), ast.NotEq: ("!=", operator.ne),
    ast.Lt: ("<", operator.lt), ast.LtE: ("<=", operator.le),
    ast.Gt: (">", operator.gt), ast.GtE: (">=", operator.ge),
    ast.Is: ("is", operator.is_), ast.IsNot: ("is not", operator.is_not),
    ast.In: ("in", lambda a, b: a in b), ast.NotIn: ("not in", lambda a, b: a not in b),
}

_current = [None]
_sites = []  # sid -> (rel, line, col, opname)
_funcs = []  # sid -> callable
_records = {}  # sid -> {ctx: set | None}
_enums = {}
_type_rep = {}
PRIMS = (type(None), bool, int, float, str, bytes)


def _identity_eq(tp):
    return getattr(tp, "__eq__", None) is object.__eq__ and getattr(tp, "__ne__", None) is object.__ne__


class _Unknown(Exception):
    pass


def _enum_rep(x):
    tp = type(x)
    name = f"{tp.__module__}.{tp.__qualname__}"
    if name not in _enums:
        members = {}
        for m in tp:
            try:
                members[m.name] = _rep(m.value, depth=1)
            except _Unknown:
                members[m.name] = ("?",)
        for alias, m in tp.__members__.items():
            members.setdefault(alias, members.get(m.name, ("?",)))
        _enums[name] = {"members": members, "canonical": sorted(m.name for m in tp),
                        "identity": _identity_eq(tp),
                        "mixin": next((b.__name__ for b in tp.__mro__ if b in PRIMS), None)}
    return ("e", name, x.name)


def _rep(x, depth=0):
    tp = type(x)
    if tp in PRIMS:
        if tp in (str, bytes) and len(x) > MAX_STR:
            raise _Unknown
        return ("v", tp.__name__, x)
    if isinstance(x, enum.Enum):
        return _enum_rep(x)
    if isinstance(x, type):
        if not _identity_eq(type(x)):
            raise _Unknown
        return ("c", f"{x.__module__}.{x.__qualname__}")
    if tp in (tuple, list, set, frozenset) and depth == 0:
        if len(x) > MAX_ITEMS:
            raise _Unknown
        items = [_rep(e, 1) for e in x]
        for it in items:
            if it[0] == "o":
                raise _Unknown  # identity of opaque elements is lost
        if tp in (set, frozenset):
            items.sort(key=repr)
        return ("t" if tp is tuple else "l" if tp is list else "s", tuple(items))
    r = _type_rep.get(tp)
    if r is None:
        r = _type_rep[tp] = ("o", f"{tp.__module__}.{tp.__qualname__}", _identity_eq(tp))
    return r


def _br_cmp_(sid, a, b):
    ctx = _current[0] or "IMPORT"
    per = _records.get(sid)
    if per is None:
        per = _records[sid] = {}
    seen = per.get(ctx, ())
    if seen is not None:
        if not seen:
            seen = per[ctx] = set()
        if len(seen) <= CAP:
            try:
                seen.add((_rep(a), _rep(b), a is b))
            except _Unknown:
                per[ctx] = None
            except Exception:
                per[ctx] = None
            else:
                if len(seen) > CAP:
                    per[ctx] = None
    return _funcs[sid](a, b)


builtins._br_cmp_ = _br_cmp_

# ---- dict literals bound at module or class level: which keys each test reads.
# A value-only edit to one entry can only reach tests that read that key (or read the
# dict in bulk). Reads while importing are recorded too, so the selector can refuse
# entries that import-time code copied or transformed.

KEY_CAP = 512
_dict_sites = []  # did -> (rel, line, col)
_dict_reads = {}  # did -> {ctx: set(keys) | None}; None = too many keys, treat as bulk
_dict_bulk = {}  # did -> set(ctx)


def _key_rep(k):
    try:
        return _rep(k, depth=1)
    except Exception:
        return ("?",)


class _TrackDict(dict):
    __slots__ = ("_br_did",)

    def _br_key(self, k):
        ctx = _current[0] or "IMPORT"
        per = _dict_reads.setdefault(self._br_did, {})
        seen = per.get(ctx, ())
        if seen is None:
            return
        if not seen:
            seen = per[ctx] = set()
        seen.add(_key_rep(k))
        if len(seen) > KEY_CAP:
            per[ctx] = None

    def _br_all(self):
        _dict_bulk.setdefault(self._br_did, set()).add(_current[0] or "IMPORT")

    def __getitem__(self, k):
        self._br_key(k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        self._br_key(k)
        return dict.get(self, k, default)

    def pop(self, k, *default):
        self._br_key(k)
        return dict.pop(self, k, *default)

    def setdefault(self, k, default=None):
        self._br_key(k)
        return dict.setdefault(self, k, default)

    def __iter__(self):
        # overriding iteration makes {**d}, dict(d) and d2.update(d) go through keys()
        # and __getitem__, so those copies are recorded per key
        return dict.__iter__(self)

    def values(self):
        self._br_all()
        return dict.values(self)

    def items(self):
        self._br_all()
        return dict.items(self)

    def popitem(self):
        self._br_all()
        return dict.popitem(self)

    def copy(self):
        self._br_all()
        return dict.copy(self)

    def __or__(self, other):
        self._br_all()
        return dict.__or__(self, other)

    def __ror__(self, other):
        self._br_all()
        return dict.__ror__(self, other)

    def __eq__(self, other):
        self._br_all()
        return dict.__eq__(self, other)

    def __ne__(self, other):
        self._br_all()
        return dict.__ne__(self, other)

    __hash__ = None

    def __repr__(self):
        self._br_all()
        return dict.__repr__(self)

    def __reduce_ex__(self, protocol):
        self._br_all()
        return (dict, (dict(dict.items(self)),))


def _br_dict_(did, d):
    t = _TrackDict(d)
    t._br_did = did
    return t


builtins._br_dict_ = _br_dict_


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel
        self.in_function = 0

    def visit_FunctionDef(self, node):
        self.in_function += 1
        self.generic_visit(node)
        self.in_function -= 1
        return node

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

    def _wrap(self, node):
        value = node.value
        if self.in_function or not isinstance(value, ast.Dict):
            return node
        did = len(_dict_sites)
        _dict_sites.append((self.rel, value.lineno, value.col_offset))
        node.value = ast.copy_location(
            ast.Call(func=ast.Name(id="_br_dict_", ctx=ast.Load()), args=[ast.Constant(did), value], keywords=[]),
            value)
        return node

    def visit_Assign(self, node):
        self.generic_visit(node)
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            return self._wrap(node)
        return node

    def visit_AnnAssign(self, node):
        self.generic_visit(node)
        if node.value is not None and isinstance(node.target, ast.Name):
            return self._wrap(node)
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        name, fn = OPS[type(node.ops[0])]
        sid = len(_sites)
        _sites.append((self.rel, node.lineno, node.col_offset, name))
        _funcs.append(fn)
        call = ast.Call(func=ast.Name(id="_br_cmp_", ctx=ast.Load()),
                        args=[ast.Constant(sid), node.left, node.comparators[0]], keywords=[])
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = self.get_filename(fullname)
        with open(path, "rb") as fh:
            source = fh.read()
        tree = ast.parse(source, path)
        tree = _Rewrite(os.path.relpath(path, ROOT)).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True)

    def get_data(self, path):
        if path.endswith(".pyc"):
            raise OSError("no bytecode for rewritten modules")
        return super().get_data(path)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if not fullname.startswith("sqlglot"):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and spec.origin and os.path.realpath(spec.origin).startswith(PKG) \
                and spec.origin.endswith(".py"):
            spec.loader = _Loader(fullname, spec.origin)
        return spec


if any(m.startswith("sqlglot") for m in sys.modules):
    raise RuntimeError("operand_plugin must load before sqlglot is imported")
sys.meta_path.insert(0, _Finder())


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
    out = OUT
    if os.environ.get("PYTEST_XDIST_WORKER"):
        out = f"{OUT}.{os.environ['PYTEST_XDIST_WORKER']}"
    with open(out, "wb") as fh:
        pickle.dump({"sites": _sites, "records": _records, "enums": _enums,
                     "dict_sites": _dict_sites, "dict_reads": _dict_reads,
                     "dict_bulk": {k: sorted(v) for k, v in _dict_bulk.items()}}, fh, protocol=4)
