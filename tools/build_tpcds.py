"""Per-query data for tests/test_executor.py::TestExecutor::test_execute_tpcds at the pinned commit.

The test executes each TPC-DS query with sqlglot's Python executor (the parsed query is
pickled to a worker, as a process pool would) and compares the result with duckdb running
sqlglot's duckdb transpilation of the same query. If a patch changes neither output for any
query that runs the changed code, the test's outcome can't change.

This records, from the pinned commit only:
  - the sqlglot lines the table setup runs, and the lines each query runs
  - each query's CPU time without coverage
  - each query's pristine outputs: transpiled duckdb SQL, result columns and rows
  - the loaded tables, built exactly as the test's setUpClass builds them

usage: python build_tpcds.py <sqlglot repo> <out.pkl.gz>
"""
import ast
import csv
import gzip
import os
import pickle
import sys
import time

repo, out_path = sys.argv[1:3]
repo = os.path.realpath(repo)
sys.path.insert(0, repo)

import coverage  # noqa: E402

FIX = os.path.join(repo, "tests", "fixtures", "optimizer", "tpc-ds")


def open_file(file_name):
    with open(file_name, "rb") as f:
        gzipped = f.read(2) == b"\x1f\x8b"
    if gzipped:
        return gzip.open(file_name, "rt", newline="")
    return open(file_name, encoding="utf-8", newline="")


def load_rows(table):
    """Rows exactly as TestExecutor.setUpClass builds them."""
    reader = csv.reader(open_file(os.path.join(FIX, f"{table}.csv.gz")), delimiter="|")
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


def run_pass(with_coverage):
    cov = None
    if with_coverage:
        cov = coverage.Coverage(data_file=None, source=[os.path.join(repo, "sqlglot")])
        cov.start()
        cov.switch_context("setup")
    import sqlglot  # noqa: F401
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import execute
    from sqlglot.executor.table import Table
    from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs

    raw = {table: load_rows(table) for table in TPCDS_SCHEMA}
    tables = {table: Table(columns=columns, rows=raw[table]) for table, columns in TPCDS_SCHEMA.items()}
    schema, tables = pickle.loads(pickle.dumps((TPCDS_SCHEMA, tables)))
    sqls = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))

    # Split each query's coverage: lines run inside execute()'s optimize() call ("opt")
    # versus everything else ("rest"); the digest of the optimizer's output is computed
    # under its own context so its lines never count as either.
    import hashlib
    import sqlglot.executor as executor_module

    original_optimize = executor_module.optimize
    current = {"q": None}

    def optimize_split(*args, **kwargs):
        q = current["q"]
        if cov:
            cov.switch_context(f"q{q}:opt")
        try:
            result = original_optimize(*args, **kwargs)
        finally:
            if cov:
                cov.switch_context("digest")
        hashlib.sha256(repr(result.dump()).encode()).hexdigest()
        if cov:
            cov.switch_context(f"q{q}:rest")
        return result

    executor_module.optimize = optimize_split

    queries = []
    for i, (meta, sql, _) in enumerate(sqls):
        if not meta.get("execute"):
            continue
        current["q"] = i
        if cov:
            cov.switch_context(f"q{i}:rest")
        t0 = time.process_time()
        expression = pickle.loads(pickle.dumps(parse_one(sql)))
        used = {t.name: tables[t.name] for t in find_tables(expression)}
        result = pickle.loads(pickle.dumps(execute(expression, schema=schema, tables=used)))
        duck_sql = transpile(sql, write="duckdb")[0]
        cpu = time.process_time() - t0
        queries.append({"index": i, "sql": sql, "cpu": cpu, "duck_sql": duck_sql,
                        "columns": tuple(result.columns), "rows": list(result.rows)})
    data = None
    if cov:
        cov.stop()
        data = cov.get_data()
    return raw, queries, data, schema


raw, queries, data, schema = run_pass(with_coverage=True)
lines_by_context = {}
for f in data.measured_files():
    rel = os.path.relpath(os.path.realpath(f), repo)
    if not rel.startswith("sqlglot/"):
        continue
    for ctx in data.measured_contexts():
        data.set_query_context(ctx)
        got = data.lines(f) or []
        if got:
            lines_by_context.setdefault(ctx, {})[rel] = sorted(got)

# second pass, in a fresh interpreter, for CPU times and pristine outputs without coverage
import subprocess  # noqa: E402

timing_path = out_path + ".timing.pkl"
timing_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tpcds_timing.py")
subprocess.run([sys.executable, timing_script, repo, timing_path], check=True)
timed = pickle.load(open(timing_path, "rb"))
os.remove(timing_path)
by_index = {q["index"]: q for q in timed}

out = {
    "test_id": "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "setup_lines": lines_by_context.get("setup", {}),
    "schema": schema,
    "tables": {t: rows for t, rows in raw.items()},
    "digest_lines": lines_by_context.get("digest", {}),
    "queries": [
        {**by_index[q["index"]],
         "opt_lines": lines_by_context.get(f"q{q['index']}:opt", {}),
         "rest_lines": lines_by_context.get(f"q{q['index']}:rest", {})}
        for q in queries
    ],
}
with gzip.open(out_path, "wb", compresslevel=9) as fh:
    pickle.dump(out, fh, protocol=pickle.HIGHEST_PROTOCOL)
total = sum(q["cpu"] for q in out["queries"])
print(f"{len(out['queries'])} executed queries, {total:.1f}s CPU without coverage, "
      f"wrote {out_path} ({os.path.getsize(out_path) / 1e6:.2f} MB)")
sys.exit(0)
