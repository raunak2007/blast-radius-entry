"""Run a few TPC-DS queries of test_execute_tpcds against the patched sources.

test_execute_tpcds costs 21.5 s, runs 99 queries, and fails only when one of them
fails. When a patch edits only function bodies, a query that never executed a changed
line behaves exactly as at the pinned commit, where all of them pass. So running just
the queries whose coverage (maps/tpcds_queries.json.gz) touches the change decides the
whole test, usually for a few seconds of CPU.

The driver replays what the test does per query, with the test module's own helpers:
parse in the main process, pickle to the worker, execute, pickle the table back, and
compare with DuckDB's result for the transpiled SQL. Mode "map" (offline) records the
coverage of one query in a fresh process; mode "run" executes the given queries with
the patched files served from memory and prints "pass" only if every one matches.
"""

from __future__ import annotations

import json
import subprocess
import sys

TEST_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
SECONDS_PER_QUERY = 0.6  # generous: about 0.33 s CPU per executed query on the dev machine
FIXED_SECONDS = 3.0  # imports (pandas, duckdb) and loading the TPC-DS tables
PROBE_TIMEOUT_FACTOR = 4.0

DRIVER = r"""
import importlib.abc, importlib.util, json, os, pickle, sys
root, mode, indices = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
sys.path.insert(0, root)
patched = json.loads(sys.stdin.read() or "{}")
fixtures = patched.pop("__fixtures__", {})  # fixture path (under tests/fixtures/) -> patched text

class Loader(importlib.abc.Loader):
    def __init__(self, path, src):
        self.path, self.src = path, src
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        module.__file__ = os.path.join(root, self.path)
        exec(compile(self.src, module.__file__, "exec"), module.__dict__)

NAMES = {}
for rel, src in patched.items():
    name = rel[:-3].replace("/", ".")
    NAMES[name[: -len(".__init__")] if name.endswith(".__init__") else name] = (rel, src)

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name not in NAMES:
            return None
        rel, src = NAMES[name]
        is_pkg = rel.endswith("__init__.py")
        spec = importlib.util.spec_from_loader(name, Loader(rel, src), origin=os.path.join(root, rel), is_package=is_pkg)
        if is_pkg:
            spec.submodule_search_locations = [os.path.dirname(os.path.join(root, rel))]
        return spec

sys.meta_path.insert(0, Finder())
cov = None
if mode == "map":
    import coverage
    cov = coverage.Coverage(data_file=None, source=[os.path.join(root, "sqlglot")])
    cov.start()

import tests.test_executor as te
if fixtures:
    import builtins, io
    import tests.helpers as th
    _real_open = builtins.open
    _served = {os.path.normpath(os.path.join(th.FIXTURES_DIR, k)): v for k, v in fixtures.items()}
    def _open(file, *args, **kwargs):
        text = _served.get(os.path.normpath(str(file)))
        return io.StringIO(text) if text is not None else _real_open(file, *args, **kwargs)
    th.open = _open  # the helper module's global shadows the builtin for its loaders
from sqlglot import parse_one

te.TestExecutor.setUpClass()
case = te.TestExecutor("test_execute_tpcds")
schema, tables = pickle.loads(pickle.dumps((te.TPCDS_SCHEMA, case.tpcds_tables)))
te.initializer(schema, tables)
if indices == "all":
    indices = list(range(len(case.tpcds_sqls)))
for i in indices:
    args, sql, _ = case.tpcds_sqls[i]
    expression = pickle.loads(pickle.dumps(parse_one(sql)))
    table = te.mp_execute(expression, args)
    table = pickle.loads(pickle.dumps(table))
    if table is not None:
        case.subtestHelper(i, table, tpch=False)
te.TestExecutor.tearDownClass()

if cov is not None:
    cov.stop()
    data = cov.get_data()
    print(json.dumps({os.path.relpath(f, root): sorted(data.lines(f) or []) for f in data.measured_files()}))
else:
    print("pass " + str(len(case.tpcds_sqls)))
"""


def probe_env() -> dict[str, str]:
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1",
            "SKIP_INTEGRATION": "0"}


def estimated_seconds(count: int) -> float:
    return FIXED_SECONDS + SECONDS_PER_QUERY * count


def run_queries(src_root: str, patched: dict[str, str], indices: list[int] | str, python: str = sys.executable) -> bool:
    """True only when every given query passes on the patched sources."""
    count = 99 if indices == "all" else len(indices)
    timeout = PROBE_TIMEOUT_FACTOR * estimated_seconds(count) + 10
    try:
        proc = subprocess.run(
            [python, "-c", DRIVER, src_root, "run", json.dumps(indices)],
            input=json.dumps(patched), capture_output=True, text=True, timeout=timeout,
            env=probe_env(), cwd=src_root,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip().startswith("pass")


def map_query(src_root: str, index: int, python: str) -> dict[str, list[int]]:
    """Coverage of one query in a fresh process (offline map building)."""
    proc = subprocess.run(
        [python, "-c", DRIVER, src_root, "map", json.dumps([index])],
        input="{}", capture_output=True, text=True, timeout=600, env=probe_env(), cwd=src_root,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"query {index}: {proc.stderr[-500:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])
