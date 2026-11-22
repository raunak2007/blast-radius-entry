"""Re-record the per-query line sets of tpcds.pkl.gz so each query starts from a fresh worker.

build_tpcds.py ran every query one after another in a single process, so code that runs
only once (lazy imports, caches built on first use, such as the generator's dispatch table)
was credited to the first query alone. test_execute_tpcds runs its queries in a spawn pool,
where any query can be the first one a worker runs. Here each query runs in a forked copy
of a process that has only done what a fresh worker has done (the test module's imports and
the table setup), so its line sets include everything it would run as a worker's first
query. Timings and pristine outputs are kept from the input file.

usage: python build_tpcds_fresh.py <sqlglot repo> <in tpcds.pkl.gz> <out.pkl.gz>
"""
import gzip
import hashlib
import os
import pickle
import sys

repo, in_path, out_path = sys.argv[1:4]
repo = os.path.realpath(repo)
sys.path.insert(0, repo)
os.chdir(repo)

import coverage  # noqa: E402

SRC = os.path.join(repo, "sqlglot")


def by_context(data):
    out = {}
    for f in data.measured_files():
        rel = os.path.relpath(os.path.realpath(f), repo)
        if not rel.startswith("sqlglot/"):
            continue
        for ctx in data.measured_contexts():
            data.set_query_context(ctx)
            got = data.lines(f) or []
            if got:
                out.setdefault(ctx, {})[rel] = sorted(got)
    return out


old = pickle.load(gzip.open(in_path))

cov = coverage.Coverage(data_file=None, source=[SRC])
cov.start()
cov.switch_context("setup")
# A spawn worker unpickles tests.test_executor.mp_execute (importing that module and all it
# imports) and the initializer's (schema, tables); the main process parses each query.
import tests.test_executor  # noqa: E402,F401
from sqlglot import find_tables, parse_one, transpile  # noqa: E402
import sqlglot.executor as executor_module  # noqa: E402
from sqlglot.executor import execute  # noqa: E402
from sqlglot.executor.table import Table  # noqa: E402

tables = {t: Table(columns=c, rows=old["tables"][t]) for t, c in old["schema"].items()}
schema, tables = pickle.loads(pickle.dumps((old["schema"], tables)))
cov.stop()
setup = by_context(cov.get_data()).get("setup", {})
del cov

original_optimize = executor_module.optimize
results = {}
for q in old["queries"]:
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            c = coverage.Coverage(data_file=None, source=[SRC])
            i = q["index"]

            def optimize_split(*args, **kwargs):
                c.switch_context(f"q{i}:opt")
                try:
                    result = original_optimize(*args, **kwargs)
                finally:
                    c.switch_context("digest")
                digest = hashlib.sha256(repr(result.dump()).encode()).hexdigest()
                c.switch_context(f"q{i}:rest")
                optimize_split.digest = digest
                return result

            executor_module.optimize = optimize_split
            c.start()
            c.switch_context(f"q{i}:rest")
            expression = pickle.loads(pickle.dumps(parse_one(q["sql"])))
            used = {t.name: tables[t.name] for t in find_tables(expression)}
            result = pickle.loads(pickle.dumps(execute(expression, schema=schema, tables=used)))
            duck_sql = transpile(q["sql"], write="duckdb")[0]
            c.stop()
            ok = (duck_sql == q["duck_sql"] and tuple(result.columns) == q["columns"]
                  and list(result.rows) == q["rows"] and optimize_split.digest == q["opt_digest"])
            payload = pickle.dumps({"ok": ok, "lines": by_context(c.get_data())})
            view = memoryview(payload)
            while view:
                n = os.write(w, view)
                view = view[n:]
        finally:
            os._exit(0)
    os.close(w)
    chunks = []
    while True:
        b = os.read(r, 1 << 20)
        if not b:
            break
        chunks.append(b)
    os.close(r)
    os.waitpid(pid, 0)
    got = pickle.loads(b"".join(chunks))
    if not got["ok"]:
        sys.exit(f"query {q['index']}: outputs differ from the input file")
    results[q["index"]] = got["lines"]
    print(f"q{q['index']}: done", file=sys.stderr, flush=True)

digest = {}
for lines in results.values():
    for rel, ls in lines.get("digest", {}).items():
        digest.setdefault(rel, set()).update(ls)
out = {**old, "setup_lines": setup, "digest_lines": {rel: sorted(v) for rel, v in digest.items()},
       "queries": [{**q, "opt_lines": results[q["index"]].get(f"q{q['index']}:opt", {}),
                    "rest_lines": results[q["index"]].get(f"q{q['index']}:rest", {})}
                   for q in old["queries"]]}
with gzip.open(out_path, "wb", compresslevel=9) as fh:
    pickle.dump(out, fh, protocol=pickle.HIGHEST_PROTOCOL)
print(f"wrote {out_path} ({os.path.getsize(out_path) / 1e6:.2f} MB)")
