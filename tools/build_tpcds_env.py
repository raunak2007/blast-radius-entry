"""Which executor ENV entries each executed TPC-DS query of test_execute_tpcds uses.

The Python executor evaluates generated code with a copy of sqlglot.executor.env.ENV as its
globals (and looks up subquery comparison operators in that copy), so an ENV entry can only
matter to a query whose execution looks it up. For every query, from the pristine sources and
the recorded tables (tpcds.pkl.gz), this replaces every callable ENV value with a recorder that
notes its key and calls the original, records every name the compiled generated code refers to
(co_names, nested code included, via the python executor module's `compile`), executes the
query as the test's pool worker does, and checks the rows are the recorded ones.

Output: {"version": 1, "queries": {index: {"called": [keys], "names": [names]}},
         "noncallable": [keys whose values aren't callable]}

usage: python build_tpcds_env.py <sqlglot repo> <tpcds.pkl.gz> <out.pkl.gz>
"""
import builtins
import gzip
import os
import pickle
import sys
import types

repo, in_path, out_path = sys.argv[1:4]
repo = os.path.realpath(repo)
in_path, out_path = os.path.realpath(in_path), os.path.realpath(out_path)
sys.path.insert(0, repo)
os.chdir(repo)

from sqlglot import find_tables, parse_one  # noqa: E402
import sqlglot.executor.env as env_module  # noqa: E402
import sqlglot.executor.python as python_module  # noqa: E402
from sqlglot.executor import execute  # noqa: E402
from sqlglot.executor.table import Table  # noqa: E402

data = pickle.load(gzip.open(in_path))
tables = {t: Table(columns=c, rows=data["tables"][t]) for t, c in data["schema"].items()}
schema, tables = pickle.loads(pickle.dumps((data["schema"], tables)))

called, names = set(), set()


def recorder(key, fn):
    def call(*args, **kwargs):
        called.add(key)
        return fn(*args, **kwargs)

    return call


noncallable = []
for key, value in list(env_module.ENV.items()):
    if callable(value):
        env_module.ENV[key] = recorder(key, value)
    else:
        noncallable.append(key)


def all_names(code):
    out = set(code.co_names)
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            out |= all_names(c)
    return out


def recording_compile(*args, **kwargs):
    code = builtins.compile(*args, **kwargs)
    names.update(all_names(code))
    return code


python_module.compile = recording_compile

out = {}
for q in data["queries"]:
    called.clear()
    names.clear()
    expression = pickle.loads(pickle.dumps(parse_one(q["sql"])))
    used = {t.name: tables[t.name] for t in find_tables(expression)}
    result = pickle.loads(pickle.dumps(execute(expression, schema=schema, tables=used)))
    assert tuple(result.columns) == q["columns"] and list(result.rows) == q["rows"], f"rows differ for {q['index']}"
    out[q["index"]] = {"called": sorted(called), "names": sorted(names)}

with gzip.open(out_path, "wb") as fh:
    pickle.dump({"version": 1, "queries": out, "noncallable": sorted(noncallable)}, fh)
print(f"{len(out)} queries; keys used per query: {sorted(len(v['called']) for v in out.values())}; "
      f"noncallable {noncallable}")
