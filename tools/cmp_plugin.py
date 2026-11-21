"""pytest plugin: weak-mutation data for sqlglot's comparisons, at the pinned commit.

Every sqlglot module is compiled with each single comparison `a OP b` turned into a call
that computes the real result and, for one neighbouring operator, the result the
comparison would have had:  is -> ==,  is not -> !=,  < -> <=,  <= -> <,  > -> >=,  >= -> >.
Per test it records where the neighbour would have given a different answer (or raised).
Only comparisons whose operator has such a neighbour are rewritten; for the others, a swap
flips every result, and line coverage already says which tests ran them. If a patch swaps OP for that neighbour, only tests in the
second set can behave differently. Runs outside any test (import, collection) are kept
under "<outside>". Work in process pools runs inline, as in usage_plugin.

Output (CMP_OUT, one file per xdist worker), JSON:
  {"sites": [[rel, line, col, op], ...], "ran": {site: [ctx]}, "differs": {site: [ctx]},
   "failed": [test ids that failed in this run]}
Enable with:  CMP_OUT=<file> pytest -p cmp_plugin
"""
import ast
import builtins
import importlib.abc
import importlib.machinery
import json
import operator
import os
import sys

_current = [None]
_sites = []  # site index -> (rel, line, col, op)
_ran = {}  # site -> set(ctx)
_differs = {}  # site -> set(ctx)
_failed = set()
_root = [None]

OPS = {
    "is": operator.is_, "is not": operator.is_not, "==": operator.eq, "!=": operator.ne,
    "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
    "in": lambda a, b: a in b, "not in": lambda a, b: a not in b,
}
NAMES = {ast.Is: "is", ast.IsNot: "is not", ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=",
         ast.Gt: ">", ast.GtE: ">=", ast.In: "in", ast.NotIn: "not in"}
ALT = {"is": "==", "is not": "!=", "<": "<=", "<=": "<", ">": ">=", ">=": ">"}


_done = []  # site -> the context a difference was last recorded for (skip re-checking it)


def _make(op, alt):
    real, other = OPS[op], OPS[alt]

    def check(site, a, b):
        r = real(a, b)
        ctx = _current[0]
        if _done[site] is ctx and ctx is not None:
            return r
        try:
            same = other(a, b) is r
        except BaseException:  # noqa: BLE001  the neighbour raising counts as a difference
            same = False
        if not same:
            d = _differs.get(site)
            if d is None:
                d = _differs[site] = set()
            d.add(ctx)
            _done[site] = ctx
        return r

    return check


_CHECKS = {op: _make(op, alt) for op, alt in ALT.items()}


class _Wrap(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or NAMES.get(type(node.ops[0])) not in ALT:
            return node  # ==, !=, in, not in: a swap always flips them, line coverage says enough
        op = NAMES[type(node.ops[0])]
        site = len(_sites)
        _sites.append((self.rel, node.lineno, node.col_offset, op))
        _done.append(None)
        call = ast.Call(func=ast.Name(f"_BR_CMP_{op.replace(' ', '_').replace('<', 'lt').replace('>', 'gt').replace('=', 'e')}", ast.Load()),
                        args=[ast.Constant(site), node.left, node.comparators[0]], keywords=[])
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        source = self.get_data(self.path)
        tree = ast.parse(source, self.path)
        rel = os.path.relpath(self.path, _root[0]).replace(os.sep, "/")
        tree = _Wrap(rel).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, self.path, "exec", dont_inherit=True)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if not (name == "sqlglot" or name.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            return spec
        spec.loader = _Loader(name, spec.origin)
        return spec


def pytest_configure(config):
    assert "sqlglot" not in sys.modules, "cmp_plugin must load before sqlglot"
    import concurrent.futures

    _root[0] = str(config.rootpath)
    # no leading double underscore: class bodies would mangle the names
    for op, fn in _CHECKS.items():
        setattr(builtins, f"_BR_CMP_{op.replace(' ', '_').replace('<', 'lt').replace('>', 'gt').replace('=', 'e')}", fn)
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


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)


def pytest_runtest_logfinish(nodeid, location):
    _current[0] = None


def pytest_sessionfinish(session, exitstatus):
    out = os.environ["CMP_OUT"]
    if os.environ.get("PYTEST_XDIST_WORKER"):
        out = f"{out}.{os.environ['PYTEST_XDIST_WORKER']}"

    def name(c):
        return "<outside>" if c is None else c

    with open(out, "w") as fh:
        json.dump({
            "sites": _sites,
            "ran": {},
            "differs": {str(k): sorted(map(name, v)) for k, v in _differs.items()},
            "failed": sorted(_failed),
        }, fh)
