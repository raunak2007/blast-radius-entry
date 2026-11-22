"""Compile per-test trace records into the selector's inverted maps.

Input: the directory written by tools/trace_tests.py (one JSON per test number)
and the tests.json the trace was made from. Output: maps/coverage.json.gz with

  tests:  list of node ids (index = position in this list)
  cost:   per-test measured CPU seconds of the test body in the tracing run
  sets:   distinct test sets, each a hex bitmask over test indexes
  func:   {file: {line: set_index}}   lines run by function code
  mod:    {file: {line: set_index}}   lines run by module/class-body code
  entry:  {file: {firstlineno: set_index}}   functions entered
  opens:  {path: set_index}           repository files opened as data
  lists:  {dir: set_index}            directories listed outside imports
  imports:{file: set_index}           tests that imported the module (any mod line)

Usage: python tools/build_maps.py <trace dir> <tests.json> <out file> [cpu.json scale]

cpu.json (from tools/measure_cpu.py) gives each test's CPU seconds as our verifier
runs it; divided by `scale` (measuring machine / labeling machine speed, estimated
from single-threaded tests) it becomes `vcpu`, the verification cost estimate.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from collections import defaultdict


def main():
    trace_dir, tests_json, out = sys.argv[1:4]
    cpu_measured = json.load(open(sys.argv[4])) if len(sys.argv) > 4 else {}
    scale = float(sys.argv[5]) if len(sys.argv) > 5 else 1.0
    tests = json.load(open(tests_json))["tests"]
    ids = [t["id"] for t in tests]
    index = {node: i for i, node in enumerate(ids)}

    func = defaultdict(lambda: defaultdict(int))
    mod = defaultdict(lambda: defaultdict(int))
    entry = defaultdict(lambda: defaultdict(int))
    opens = defaultdict(int)
    lists = defaultdict(int)
    imports = defaultdict(int)
    inst = defaultdict(int)
    cost = [None] * len(ids)
    wall = [None] * len(ids)
    missing = []
    clean_failures = []
    for t in tests:
        path = os.path.join(trace_dir, f"{t['number']}.json")
        if not os.path.exists(path):
            missing.append(t["id"])
            continue
        record = json.load(open(path))
        assert record["id"] == t["id"], (record["id"], t["id"])
        bit = 1 << index[t["id"]]
        if record["status"].get("failed"):
            clean_failures.append(t["id"])
        cost[index[t["id"]]] = record["cpu"]
        wall[index[t["id"]]] = record["status"].get("duration")
        for f, lines in record["func_lines"].items():
            bucket = func[f]
            for line in lines:
                bucket[line] |= bit
        for f, lines in record["mod_lines"].items():
            bucket = mod[f]
            for line in lines:
                bucket[line] |= bit
            imports[f] |= bit
        for f, keys in record["entered"].items():
            bucket = entry[f]
            for key in keys:
                first = int(key.split(":", 1)[0])
                bucket[first] |= bit
        for f in record["opens"]:
            opens[f] |= bit
        for d in record["lists"]:
            lists[d] |= bit
        for k in record.get("inst", []):
            inst[k] |= bit

    sets: list[int] = []
    set_index: dict[int, int] = {}

    def intern(mask: int) -> int:
        k = set_index.get(mask)
        if k is None:
            k = set_index[mask] = len(sets)
            sets.append(mask)
        return k

    def table(nested):
        return {f: {str(line): intern(mask) for line, mask in sorted(lines.items())} for f, lines in sorted(nested.items())}

    data = {
        "tests": ids,
        "cost": cost,
        "vcpu": [round(cpu_measured[i]["cpu"] / scale, 4) if i in cpu_measured and cpu_measured[i].get("cpu") is not None else None for i in ids],
        "wall": wall,
        "func": table(func),
        "mod": table(mod),
        "entry": table(entry),
        "opens": {f: intern(m) for f, m in sorted(opens.items())},
        "lists": {f: intern(m) for f, m in sorted(lists.items())},
        "imports": {f: intern(m) for f, m in sorted(imports.items())},
        "inst": {k: intern(m) for k, m in sorted(inst.items())},
    }
    data["sets"] = [format(m, "x") for m in sets]
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with gzip.open(out, "wt", compresslevel=9) as handle:
        json.dump(data, handle, separators=(",", ":"))
    print(f"tests {len(ids)} missing {len(missing)} clean-failures {clean_failures} sets {len(sets)} "
          f"files {len(func)} size {os.path.getsize(out)}", file=sys.stderr)
    if missing:
        print("missing:", missing[:10], file=sys.stderr)


if __name__ == "__main__":
    main()
