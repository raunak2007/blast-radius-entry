"""Timing pass for build_tpcds.py: run each executed TPC-DS query once without coverage,
recording CPU time and the pristine outputs. usage: python tpcds_timing.py <sqlglot repo> <out.pkl>"""
import os, pickle, sys, time, gzip, csv, ast
repo, timing_path = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo)
from sqlglot import find_tables, parse_one, transpile
from sqlglot.executor import execute
from sqlglot.executor.table import Table
from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs
FIX = os.path.join(repo, "tests", "fixtures", "optimizer", "tpc-ds")


def _open(file_name):
    with open(file_name, "rb") as f:
        gz = f.read(2) == b"\x1f\x8b"
    return gzip.open(file_name, "rt", newline="") if gz else open(file_name, encoding="utf-8", newline="")


def _rows(table):
    reader = csv.reader(_open(os.path.join(FIX, f"{table}.csv.gz")), delimiter="|")
    rows, ctypes = [], []
    next(reader)
    for row in reader:
        if not ctypes:
            for v in row:
                try:
                    ctypes.append(type(ast.literal_eval(v)))
                except (ValueError, SyntaxError):
                    ctypes.append(str)
        rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
    return rows


tables = {t: Table(columns=c, rows=_rows(t)) for t, c in TPCDS_SCHEMA.items()}
schema, tables = pickle.loads(pickle.dumps((TPCDS_SCHEMA, tables)))
import hashlib
import sqlglot.executor as executor_module

original_optimize = executor_module.optimize
seen = {}


def optimize_capture(*args, **kwargs):
    result = original_optimize(*args, **kwargs)
    seen["end"] = time.process_time()
    seen["digest"] = hashlib.sha256(repr(result.dump()).encode()).hexdigest()
    seen["after_digest"] = time.process_time()
    return result


executor_module.optimize = optimize_capture
timed = []
for i, (meta, sql, _) in enumerate(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")):
    if not meta.get("execute"):
        continue
    t0 = time.process_time()
    expression = pickle.loads(pickle.dumps(parse_one(sql)))
    used = {t.name: tables[t.name] for t in find_tables(expression)}
    result = pickle.loads(pickle.dumps(execute(expression, schema=schema, tables=used)))
    t1 = time.process_time()
    duck_sql = transpile(sql, write="duckdb")[0]
    t2 = time.process_time()
    digest_cost = seen["after_digest"] - seen["end"]
    timed.append({"index": i, "sql": sql, "cpu": (t2 - t0) - digest_cost,
                  "front_cpu": (seen["after_digest"] - t0) + (t2 - t1),
                  "opt_digest": seen["digest"], "duck_sql": duck_sql,
                  "columns": tuple(result.columns), "rows": list(result.rows)})
pickle.dump(timed, open(timing_path, "wb"))
