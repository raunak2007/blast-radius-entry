"""Add, per TPC-DS query, what lets the replay settle an optimizer output that differs from
the pristine one only in node metadata (`Expr.meta`): the digest of that output with the
metadata left out, and whether the rest of execute() (the debug-log rendering, planning
and execution) ever reads metadata for this query.

Metadata is read through the `_meta` slot. Here that slot's descriptor is swapped for one
that notes every read made outside copy(), deepcopy, serde's dump/load and pickling, which
only carry metadata along. A query whose rendering, planning and execution make no such
read on the pristine optimizer output takes exactly the same path on any output that
differs only in metadata: until a metadata read, every value it computes is the same.

usage: python build_tpcds_meta.py <sqlglot repo> <in tpcds.pkl.gz> <out.pkl.gz>
"""
import gzip
import hashlib
import os
import pickle
import sys

repo, in_path, out_path = sys.argv[1:4]
repo = os.path.realpath(repo)
in_path, out_path = os.path.realpath(in_path), os.path.realpath(out_path)
sys.path.insert(0, repo)
os.chdir(repo)

import tests.test_executor  # noqa: E402,F401
from sqlglot import find_tables, parse_one  # noqa: E402
import sqlglot.executor as executor_module  # noqa: E402
from sqlglot.executor.python import PythonExecutor  # noqa: E402
from sqlglot.executor.table import Table, ensure_tables  # noqa: E402
from sqlglot.expressions.core import Expression  # noqa: E402
from sqlglot.planner import Plan  # noqa: E402
from sqlglot.schema import ensure_schema  # noqa: E402
from sqlglot.serde import META, TYPE  # noqa: E402

data = pickle.load(gzip.open(in_path))
tables = {t: Table(columns=c, rows=data["tables"][t]) for t, c in data["schema"].items()}
schema, tables = pickle.loads(pickle.dumps((data["schema"], tables)))


def strip_meta(payloads):
    out = []
    for p in payloads:
        p = {k: v for k, v in p.items() if k != META}
        if TYPE in p:
            p[TYPE] = strip_meta(p[TYPE])
        out.append(p)
    return out


def nometa_digest(expression):
    return hashlib.sha256(repr(strip_meta(expression.dump())).encode()).hexdigest()


slot = Expression.__dict__["_meta"]
CARRY = {"copy", "__deepcopy__", "dump", "load", "__reduce__"}
state = {"on": False, "reads": 0}


class Watch:
    def __get__(self, obj, typ=None):
        if obj is None:
            return self
        if state["on"] and sys._getframe(1).f_code.co_name not in CARRY:
            state["reads"] += 1
        return slot.__get__(obj, typ)

    def __set__(self, obj, value):
        slot.__set__(obj, value)

    def __delete__(self, obj):
        slot.__delete__(obj)


type.__setattr__(Expression, "_meta", Watch())
blind = 0
for q in data["queries"]:
    pid_r, pid_w = os.pipe()
    pid = os.fork()
    if pid == 0:  # a fresh copy per query, as a pool worker would be
        os.close(pid_r)
        code = 0
        try:
            expression = pickle.loads(pickle.dumps(parse_one(q["sql"])))
            used = {t.name: tables[t.name] for t in find_tables(expression)}
            tables_ = ensure_tables(used, dialect=None)
            optimized = executor_module.optimize(expression, ensure_schema(schema), leave_tables_isolated=True)
            full = hashlib.sha256(repr(optimized.dump()).encode()).hexdigest()
            assert full == q["opt_digest"], "optimizer output differs from the input file"
            nometa = nometa_digest(optimized)
            state["on"] = True
            optimized.sql(pretty=True)  # execute()'s debug-log line
            plan = Plan(optimized)
            result = pickle.loads(pickle.dumps(PythonExecutor(tables=tables_).execute(plan)))
            state["on"] = False
            assert tuple(result.columns) == q["columns"] and list(result.rows) == q["rows"], "rows differ"
            payload = pickle.dumps((nometa, state["reads"]))
        except BaseException as e:  # noqa: BLE001
            payload = pickle.dumps(("error", repr(e)))
            code = 1
        os.write(pid_w, payload)
        os._exit(code)
    os.close(pid_w)
    buf = b""
    while chunk := os.read(pid_r, 1 << 16):
        buf += chunk
    os.close(pid_r)
    os.waitpid(pid, 0)
    nometa, reads = pickle.loads(buf)
    if nometa == "error":
        sys.exit(f"query {q['index']}: {reads}")
    q["opt_digest_nometa"] = nometa
    q["meta_blind"] = reads == 0
    blind += reads == 0
    print(f"q{q['index']}: metadata reads after optimize() {reads}", file=sys.stderr, flush=True)

data["meta"] = 1
with gzip.open(out_path, "wb", compresslevel=9) as fh:
    pickle.dump(data, fh, protocol=pickle.HIGHEST_PROTOCOL)
print(f"{blind} of {len(data['queries'])} queries never read metadata after optimize(); wrote {out_path}")
