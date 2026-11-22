"""Which executor ENV entries each TPC-DS query of test_execute_tpcds looks up.

The Python executor copies ``sqlglot.executor.env.ENV`` into the globals its generated code is
evaluated in (``Context.env``) and into ``PythonExecutor.env`` (``self.env[op]``). Every
query the test executes is run here exactly as ``tpcds_check.fingerprints`` runs it, with both
dicts replaced by copies that note each key looked up. A query that never looks up a key can
only meet that entry's value by its key, so an edit of that one entry cannot change its
result. Written at image build time:

    env_queries.py <sources> <out.json>   ->   {"keys": {key: [query, ...]}, "executed": [...]}

Never fails the build: without the file the selector keeps its whole-test answer.
"""

import json
import pickle
import sys
import unittest  # noqa: F401  sqlglot's strict argument checks are on under tests, so they are here


class _Noting(dict):
    seen = set()

    def __getitem__(self, key):
        _Noting.seen.add(key)
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        _Noting.seen.add(key)
        return dict.get(self, key, default)

    def __contains__(self, key):
        _Noting.seen.add(key)
        return dict.__contains__(self, key)


def build(root, target):
    sys.path.insert(0, root)
    import tpcds_check
    from sqlglot import find_tables, parse_one
    from sqlglot.executor import context as context_module
    from sqlglot.executor import execute
    from sqlglot.executor import python as python_module
    from sqlglot.executor.env import ENV
    from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, load_sql_fixture_pairs

    def noting(cls):
        init = cls.__init__

        def __init__(self, *args, **kwargs):
            init(self, *args, **kwargs)
            self.env = _Noting(self.env)

        cls.__init__ = __init__

    noting(context_module.Context)
    noting(python_module.PythonExecutor)

    sqls = list(load_sql_fixture_pairs(tpcds_check.FIXTURE))
    schema, tables = pickle.loads(pickle.dumps((TPCDS_SCHEMA, tpcds_check._tables(FIXTURES_DIR, TPCDS_SCHEMA))))
    keys = {key: [] for key in ENV}
    executed = []
    for k, (meta, sql, _) in enumerate(sqls):
        if not meta.get("execute"):
            continue
        executed.append(k)
        _Noting.seen = set()
        expression = pickle.loads(pickle.dumps(parse_one(sql)))
        found = {t.name: tables[t.name] for t in find_tables(expression)}
        execute(expression, schema=schema, tables=found)
        for key in _Noting.seen:
            if key in keys:
                keys[key].append(k)
    with open(target, "w") as f:
        json.dump({"keys": keys, "executed": executed}, f)
    print("env_queries: %d queries, %d of %d ENV keys used" % (len(executed), sum(1 for v in keys.values() if v), len(keys)),
          file=sys.stderr)


if __name__ == "__main__":
    try:
        build(sys.argv[1], sys.argv[2])
    except Exception as error:  # noqa: BLE001
        print("env_queries: no map:", repr(error), file=sys.stderr)
