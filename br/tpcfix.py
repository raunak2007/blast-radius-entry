"""Narrow an edit of the TPC-H / TPC-DS fixture files to the tests that read what changed.

Every test of tests/test_executor.py::TestExecutor reads both files (setUpClass loads
them with tests.helpers.load_sql_fixture_pairs), but only a few use their pairs:

* test_optimizer's test_tpch / test_tpcds: meta, sql and expected of every pair;
* test_executor's test_optimized_tpch: sql and expected of every TPC-H pair;
* test_executor's test_execute_tpch / test_execute_tpcds: meta and sql of every pair.

So the pairs are rebuilt from the old and new text with the helper's own splitting. If
the new text no longer loads, or the number of pairs changed, every reader stays (their
setUpClass or their indexing can break). Otherwise only the tests reading a changed part
of a changed pair stay, and for test_execute_tpcds the changed pairs are recorded so the
selector can replay just those queries.
"""

from __future__ import annotations

import os

FIXTURES = "tests/fixtures/"
TPCH = "tests/fixtures/optimizer/tpc-h/tpc-h.sql"
TPCDS = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"
TESTS = {
    TPCH: {"all": ["tests/test_optimizer.py::TestOptimizer::test_tpch",
                   "tests/test_executor.py::TestExecutor::test_optimized_tpch"],
           "sql": ["tests/test_executor.py::TestExecutor::test_execute_tpch"]},
    TPCDS: {"all": ["tests/test_optimizer.py::TestOptimizer::test_tpcds"],
            "sql": ["tests/test_executor.py::TestExecutor::test_execute_tpcds"]},
}


def _filter_comments(s: str) -> str:
    return "\n".join([line for line in s.splitlines() if line and not line.startswith("--")])


def _extract_meta(sql: str):
    meta = {}
    sql_lines = sql.split("\n")
    i = 0
    while sql_lines[i].startswith("#"):
        key, val = sql_lines[i].split(":", maxsplit=1)
        meta[key.lstrip("#").strip()] = val.strip()
        i += 1
    return "\n".join(sql_lines[i:]), meta


def pairs(text: str) -> list[tuple[dict, str, str]]:
    """tests.helpers.load_sql_fixture_pairs on a text instead of a file."""
    statements = _filter_comments(text).split(";")
    out = []
    for i in range(0, len(statements), 2):
        if i + 1 < len(statements):
            sql, meta = _extract_meta(statements[i].strip())
            out.append((meta, sql, statements[i + 1].strip()))
    return out


def narrow(src_root: str, path: str, new_lines: list[str], numbers: dict[str, int]):
    """(tests, changed sql indices) for an edit of `path`, or None when it can't be narrowed."""
    if path not in TESTS:
        return None
    try:
        with open(os.path.join(src_root, path), encoding="utf-8") as handle:
            old = pairs(handle.read())
        new = pairs("\n".join(new_lines))
    except Exception:  # noqa: BLE001 - the helper raises too: setUpClass fails, keep every reader
        return None
    if len(old) != len(new):
        return None
    changed = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
    sql_changed = [i for i in changed if old[i][:2] != new[i][:2]]
    names = (TESTS[path]["all"] if changed else []) + (TESTS[path]["sql"] if sql_changed else [])
    if any(name not in numbers for name in names):
        return None
    return {numbers[name] for name in names}, sql_changed
