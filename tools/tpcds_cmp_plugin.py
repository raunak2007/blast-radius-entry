"""pytest plugin, loaded after cmp_plugin: its weak-mutation records for test_execute_tpcds,
per TPC-DS query (build_tpcds_cmp.py).

cmp_plugin files every evaluation under the running test. Inside test_execute_tpcds this
switches its context to the query being worked on: the test parses query i (parse_one in
the test module), runs it in the (inline) pool, and later compares its rows with duckdb's
on the transpiled SQL (subtestHelper(i)); each of those runs under "Q<i>". Everything else
in the test (pool start, the initializer) stays under the test id. It also notes, per
site, which contexts evaluated it at all.

Output (TPCDS_CMP_OUT), JSON: {"sites": [[rel, line, col, op], ...],
  "ran": {site: [ctx]}, "differs": {site: [ctx]}, "failed": [...]}
"""
import builtins
import json
import os

import pytest

TARGET = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
_ran = {}
_ctxs = []  # interned query contexts (cmp_plugin compares them by identity)


def _cmp():
    import cmp_plugin

    return cmp_plugin


def _ctx_name(i):
    while len(_ctxs) <= i:
        _ctxs.append(f"Q{len(_ctxs)}")
    return _ctxs[i]


def _wrap(fn):
    cp = _cmp()

    def inner(site, *args):
        ctx = cp._current[0]
        s = _ran.get(site)
        if s is None:
            s = _ran[site] = set()
        s.add(ctx)
        return fn(site, *args)

    return inner


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    for name in list(vars(builtins)):
        if name.startswith("_BR_CMP_") or name in ("_BR_AND", "_BR_OR"):
            setattr(builtins, name, _wrap(getattr(builtins, name)))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    if item.nodeid != TARGET:
        yield
        return
    cp = _cmp()
    mod = item.module
    count = [0]
    real_parse = mod.parse_one
    real_helper = type(item.instance).subtestHelper

    def parse_one(*args, **kwargs):
        cp._current[0] = _ctx_name(count[0])
        count[0] += 1
        return real_parse(*args, **kwargs)

    def subtest_helper(self, i, table, tpch=True):
        cp._current[0] = _ctx_name(i)
        try:
            return real_helper(self, i, table, tpch=tpch)
        finally:
            cp._current[0] = item.nodeid  # the very object cmp_plugin sets

    mod.parse_one = parse_one
    type(item.instance).subtestHelper = subtest_helper
    try:
        yield
    finally:
        mod.parse_one = real_parse
        type(item.instance).subtestHelper = real_helper


def pytest_sessionfinish(session, exitstatus):
    cp = _cmp()

    def name(c):
        return "<outside>" if c is None else c

    with open(os.environ["TPCDS_CMP_OUT"], "w") as fh:
        json.dump({
            "sites": cp._sites,
            "ran": {str(k): sorted(map(name, v)) for k, v in _ran.items()},
            "differs": {str(k): sorted(map(name, v)) for k, v in cp._differs.items()},
            "failed": sorted(cp._failed),
        }, fh)
