"""Write maps/tpcds_queries.json.gz: which TPC-DS queries of test_execute_tpcds run each line.

Each query runs in a fresh interpreter under coverage (imports, table loading and the
pickle round trips included), so a line that only runs once per process, such as a
lazy import or a cache fill, is credited to every query rather than to the first.

Run from the blast-radius folder (sqlglot checkout at the pinned commit, with the
requirements.lock packages and coverage installed):
  python tools/tpcds_map.py work/sqlglot maps
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from br.probe import map_query  # noqa: E402

QUERIES = 99
WORKERS = 8


def main() -> None:
    src, maps = os.path.abspath(sys.argv[1]), sys.argv[2]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(lambda i: map_query(src, i, sys.executable), range(QUERIES)))
    lines: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for index, files in enumerate(results):
        for path, numbers in files.items():
            for number in numbers:
                lines[path][str(number)].append(index)
    out = {"queries": QUERIES, "lines": lines}
    with gzip.open(os.path.join(maps, "tpcds_queries.json.gz"), "wt", encoding="utf-8") as handle:
        json.dump(out, handle, separators=(",", ":"))
    every = sum(1 for f in lines.values() for qs in f.values() if len(qs) == QUERIES)
    total = sum(len(f) for f in lines.values())
    print(f"{total} lines, {every} run by every query", file=sys.stderr)


if __name__ == "__main__":
    main()
