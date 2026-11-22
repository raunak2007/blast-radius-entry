"""Add TPC-DS stage records (tools/trace_stage.py) to maps/coverage.json.gz (build-time tool).

For each case, an "outer" pseudo-test: the lines and function entries the case
runs outside optimize() calls; class instantiations, opened files and listed
directories are copied from the case's full pseudo-test (so class-usage and
data-file rules can only reach an outer pseudo-test more often, never less).

Adds map["stage"][test] = {"module": ..., "attr": ..., "fixture": ...,
  "outer": [pseudo index per case], "calls": [fingerprint triples per case, or
  null when the two recordings (different hash seeds) disagree or the case
  failed], "cpu": [CPU of the case stopped after optimize, / scale]}

Usage: python tools/augment_stage.py <coverage in> <stage dir> <out> <scale>
"""

import gzip
import json
import os
import sys

TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


def main():
    src, stage_dir, out, scale = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
    data = json.load(gzip.open(src, "rt"))
    if "stage" in data:
        sys.exit("input already has stage records")
    sets = [int(h, 16) for h in data["sets"]]
    index = {m: k for k, m in enumerate(sets)}

    def intern(mask):
        k = index.get(mask)
        if k is None:
            k = index[mask] = len(sets)
            sets.append(mask)
        return k

    def unpack(table):
        return {f: {line: sets[k] for line, k in bucket.items()} for f, bucket in table.items()}

    func, mod, entry = unpack(data["func"]), unpack(data["mod"]), unpack(data["entry"])
    flat = {name: {f: sets[k] for f, k in data[name].items()} for name in ("opens", "lists", "imports", "inst")}
    info = data["cases"][TEST]
    (fixture, full_indexes), = info["fixtures"].items()
    n = len(full_indexes)
    outer, calls, cpu = [], [], []
    nondeterministic = 0
    for k in range(n):
        trace = json.load(open(os.path.join(stage_dir, f"trace_{k}.json")))
        measure = json.load(open(os.path.join(stage_dir, f"measure_{k}.json")))
        assert trace["ordinal"] == k and measure["ordinal"] == k and trace["fixture"] == fixture
        i = len(data["tests"])
        data["tests"].append(f"{TEST}[outer:{fixture}#{k}]")
        bit = 1 << i
        for f, lines in trace["func_lines"].items():
            b = func.setdefault(f, {})
            for line in lines:
                b[str(line)] = b.get(str(line), 0) | bit
        for f, lines in trace["mod_lines"].items():
            b = mod.setdefault(f, {})
            for line in lines:
                b[str(line)] = b.get(str(line), 0) | bit
            flat["imports"][f] = flat["imports"].get(f, 0) | bit
        for f, keys in trace["entered"].items():
            b = entry.setdefault(f, {})
            for key in keys:
                first = key.split(":", 1)[0]
                b[first] = b.get(first, 0) | bit
        full_bit = 1 << full_indexes[k]
        for name in ("opens", "lists", "inst"):
            for key, mask in flat[name].items():
                if mask & full_bit:
                    flat[name][key] = mask | bit
        for name in ("vcpu", "cost", "wall"):
            data[name].append(None)
        outer.append(i)
        ok = not trace["failed"] and trace["calls"] == measure["calls"]
        if not ok:
            nondeterministic += 1
        calls.append(trace["calls"] if ok else None)
        cpu.append(round(measure["body_cpu"] / scale, 4))
    warm = 0.0
    all_path = os.path.join(stage_dir, "measure_all_-1.json")
    if os.path.exists(all_path):
        # every case in one run vs each alone: the shared warm-up of a run
        together = json.load(open(all_path))["body_cpu"] / scale
        warm = max(0.0, (sum(cpu) - together) / (n - 1))
        print(f"all cases in one run {together:.2f}, alone summed {sum(cpu):.2f}, warm-up {warm:.3f}", file=sys.stderr)
    data.update({
        "func": {f: {line: intern(m) for line, m in sorted(b.items(), key=lambda x: int(x[0]))} for f, b in sorted(func.items())},
        "mod": {f: {line: intern(m) for line, m in sorted(b.items(), key=lambda x: int(x[0]))} for f, b in sorted(mod.items())},
        "entry": {f: {line: intern(m) for line, m in sorted(b.items(), key=lambda x: int(x[0]))} for f, b in sorted(entry.items())},
        "opens": {f: intern(m) for f, m in sorted(flat["opens"].items())},
        "lists": {f: intern(m) for f, m in sorted(flat["lists"].items())},
        "imports": {f: intern(m) for f, m in sorted(flat["imports"].items())},
        "inst": {f: intern(m) for f, m in sorted(flat["inst"].items())},
        "stage": {TEST: {"module": "sqlglot.executor", "attr": "optimize", "fixture": fixture,
                         "outer": outer, "calls": calls, "cpu": cpu, "warm": round(warm, 4)}},
    })
    data["sets"] = [format(m, "x") for m in sets]
    with gzip.open(out, "wt", compresslevel=9) as handle:
        json.dump(data, handle, separators=(",", ":"))
    print(f"{n} cases, nondeterministic or failed {nondeterministic}, size {os.path.getsize(out)}", file=sys.stderr)


if __name__ == "__main__":
    main()
