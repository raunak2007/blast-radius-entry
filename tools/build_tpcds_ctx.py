"""Per-query line sets of test_execute_tpcds, split by what checks each part, each recorded
from a fresh process state.

The test parses each query in the main process, pickles it to a spawn pool worker, which
unpickles it and runs sqlglot.executor.execute (ensure tables and schema, optimize, a debug
log line that renders the optimized SQL, plan, run), and pickles the result back; the main
process then compares it with duckdb running sqlglot's duckdb transpilation. A changed line
is checked, per query, by what it can affect:

  parse_lines  main-process parse_one                       -> the optimizer output digest
  opt_lines    inside optimize()                            -> the optimizer output digest
  duck_lines   transpile(sql, write="duckdb")               -> the transpiled SQL
  log_lines    evaluating expression.sql(pretty=True) for logger.debug, whose text is dropped
               (debug logging is off)                       -> it must not raise or change
                                                               the optimized expression
  exec_lines   pickling to and from the worker, find_tables, ensure_tables/ensure_schema,
               Plan and PythonExecutor                      -> only a full run

Every part runs in a forked copy of a process that has only done what a fresh worker has
done (the test module's imports and the table setup), so code that runs once per process
counts for every query that could be the first to run it. The planning and execution part
is recorded twice: right after this query's own optimize() and log line, and alone from a
fresh state on the pristine optimizer output, so it includes any one-time setup it would
otherwise have found already done. Timings and pristine outputs come from the input file;
log_cpu is the CPU of the log line plus a second digest, measured without coverage.

usage: python build_tpcds_ctx.py <sqlglot repo> <in tpcds.pkl.gz> <out.pkl.gz>
"""
import gzip
import hashlib
import os
import pickle
import sys
import time

repo, in_path, out_path = sys.argv[1:4]
repo = os.path.realpath(repo)
in_path, out_path = os.path.realpath(in_path), os.path.realpath(out_path)
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


def digest_of(expression):
    return hashlib.sha256(repr(expression.dump()).encode()).hexdigest()


old = pickle.load(gzip.open(in_path))

cov = coverage.Coverage(data_file=None, source=[SRC])
cov.start()
cov.switch_context("setup")
import tests.test_executor  # noqa: E402,F401
from sqlglot import find_tables, parse_one, transpile  # noqa: E402
import sqlglot.executor as executor_module  # noqa: E402
from sqlglot.executor import execute  # noqa: E402
from sqlglot.executor.python import PythonExecutor  # noqa: E402
from sqlglot.executor.table import Table, ensure_tables  # noqa: E402
from sqlglot.planner import Plan  # noqa: E402

tables = {t: Table(columns=c, rows=old["tables"][t]) for t, c in old["schema"].items()}
schema, tables = pickle.loads(pickle.dumps((old["schema"], tables)))
cov.stop()
setup = by_context(cov.get_data()).get("setup", {})
del cov

original_optimize = executor_module.optimize
real_logger = executor_module.logger


def in_child(fn):
    """Run fn() in a forked child; return what it returns (pickled through a pipe)."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        code = 0
        try:
            payload = pickle.dumps(("ok", fn()))
        except BaseException as e:  # noqa: BLE001
            payload = pickle.dumps(("error", repr(e)))
            code = 1
        try:
            view = memoryview(payload)
            while view:
                n = os.write(w, view)
                view = view[n:]
        finally:
            os._exit(code)
    os.close(w)
    chunks = []
    while True:
        b = os.read(r, 1 << 20)
        if not b:
            break
        chunks.append(b)
    os.close(r)
    os.waitpid(pid, 0)
    status, value = pickle.loads(b"".join(chunks))
    if status != "ok":
        sys.exit(f"child failed: {value}")
    return value


class SplitLogger:
    """Stands in for sqlglot.executor's logger: each `logger.debug` attribute access starts
    the log context (the arguments are evaluated next), the call itself ends it."""

    def __init__(self, cov, back):
        self.cov, self.back = cov, back

    def __getattr__(self, name):
        attr = getattr(real_logger, name)
        if name != "debug":
            return attr
        self.cov.switch_context("log")

        def debug(*args, **kwargs):
            self.cov.switch_context(self.back)
            return attr(*args, **kwargs)

        return debug


def main_side(q):
    c = coverage.Coverage(data_file=None, source=[SRC])
    c.start()
    c.switch_context("parse")
    expression = parse_one(q["sql"])
    c.switch_context("pre")
    blob = pickle.dumps(expression)
    c.switch_context("duck")
    duck_sql = transpile(q["sql"], write="duckdb")[0]
    c.stop()
    assert duck_sql == q["duck_sql"], "duckdb SQL differs from the input file"
    return {"lines": by_context(c.get_data()), "blob": blob}


def worker_side(q, blob):
    c = coverage.Coverage(data_file=None, source=[SRC])
    state = {}

    def optimize_split(*args, **kwargs):
        c.switch_context("opt")
        try:
            result = original_optimize(*args, **kwargs)
        finally:
            c.switch_context("digest")
        state["digest"] = digest_of(result)
        c.switch_context("ignore")
        state["optimized"] = pickle.dumps(result)
        c.switch_context("exec")
        return result

    executor_module.optimize = optimize_split
    executor_module.logger = SplitLogger(c, "exec")
    c.start()
    c.switch_context("pre")
    expression = pickle.loads(blob)
    used = {t.name: tables[t.name] for t in find_tables(expression)}
    result = pickle.loads(pickle.dumps(execute(expression, schema=schema, tables=used)))
    c.stop()
    assert state["digest"] == q["opt_digest"], "optimizer output differs from the input file"
    assert tuple(result.columns) == q["columns"] and list(result.rows) == q["rows"], "rows differ"
    lines = by_context(c.get_data())
    lines.pop("ignore", None)
    return {"lines": lines, "optimized": state["optimized"], "used": sorted(used)}


def exec_alone(q, optimized, used_names):
    """Plan and run the pristine optimizer output from a fresh state, as execute() does after
    optimize(); unpickling it counts too (more lines is the safe side)."""
    c = coverage.Coverage(data_file=None, source=[SRC])
    c.start()
    c.switch_context("exec")
    expression = pickle.loads(optimized)
    tables_ = ensure_tables({n: tables[n] for n in used_names}, dialect=None)
    plan = Plan(expression)
    result = pickle.loads(pickle.dumps(PythonExecutor(tables=tables_).execute(plan)))
    c.stop()
    assert tuple(result.columns) == q["columns"] and list(result.rows) == q["rows"], "rows differ (alone)"
    return by_context(c.get_data())


def log_timing(q):
    """CPU of the replay's log-line check (render, then digest again), without coverage."""
    expression = pickle.loads(pickle.dumps(parse_one(q["sql"])))
    used = {t.name: tables[t.name] for t in find_tables(expression)}
    tables_ = ensure_tables(used, dialect=None)
    from sqlglot.schema import ensure_schema
    optimized = original_optimize(expression, ensure_schema(schema), leave_tables_isolated=True)
    t0 = time.process_time()
    optimized.sql(pretty=True)
    digest_of(optimized)
    del tables_
    return time.process_time() - t0


out_queries = []
digest_union = {}
for q in old["queries"]:
    i = q["index"]
    main = in_child(lambda: main_side(q))
    work = in_child(lambda: worker_side(q, main["blob"]))
    alone = in_child(lambda: exec_alone(q, work["optimized"], work["used"]))
    log_cpu = in_child(lambda: log_timing(q))
    parts = {k: {} for k in ("parse", "pre", "duck", "opt", "log", "exec", "digest")}
    for src_lines in (main["lines"], work["lines"], alone):
        for ctx, files in src_lines.items():
            for rel, ls in files.items():
                parts[ctx].setdefault(rel, set()).update(ls)
    for rel, ls in parts.pop("digest").items():
        digest_union.setdefault(rel, set()).update(ls)
    exec_lines = {}
    for ctx in ("pre", "exec"):
        for rel, ls in parts[ctx].items():
            exec_lines.setdefault(rel, set()).update(ls)

    def srt(d):
        return {rel: sorted(ls) for rel, ls in d.items()}

    rest = {}
    for ctx in ("parse", "pre", "duck", "log", "exec"):
        for rel, ls in parts[ctx].items():
            rest.setdefault(rel, set()).update(ls)
    out_queries.append({**q, "log_cpu": log_cpu,
                        "parse_lines": srt(parts["parse"]), "duck_lines": srt(parts["duck"]),
                        "opt_lines": srt(parts["opt"]), "log_lines": srt(parts["log"]),
                        "exec_lines": srt(exec_lines),
                        # the v8 split, kept for older selectors: everything outside optimize()
                        "rest_lines": srt(rest)})
    print(f"q{i}: log {log_cpu * 1000:.0f} ms", file=sys.stderr, flush=True)

out = {**old, "setup_lines": setup, "digest_lines": {rel: sorted(v) for rel, v in digest_union.items()},
       "queries": out_queries, "contexts": 2}
with gzip.open(out_path, "wb", compresslevel=9) as fh:
    pickle.dump(out, fh, protocol=pickle.HIGHEST_PROTOCOL)
print(f"wrote {out_path} ({os.path.getsize(out_path) / 1e6:.2f} MB)")
