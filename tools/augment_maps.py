"""Add per-case pseudo-tests to maps/coverage.json.gz (build-time tool).

Each record of tools/trace_cases.py (one fixture-loop test restricted to one
case, or to no case at all = "outside") becomes a pseudo-test: an extra index
in the coverage maps with its own lines, entries, opens and instantiations.
Pseudo-test ids are not in tests.json, so they never reach an answer; the
selector only uses them to tell which cases of a test can see a change.

Adds to the map:
  cases: {test id: {"outside": index, "fixtures": {fixture: [index of ordinal 0, 1, ...]}}}
and vcpu for every pseudo-test (untraced CPU of the restricted run / scale).

Usage: python tools/augment_maps.py <coverage.json.gz in> <trace dir> <out> <scale>
"""

import glob
import gzip
import json
import os
import sys


def main():
    src, trace_dir, out, scale = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
    data = json.load(gzip.open(src, "rt"))
    if "cases" in data:
        sys.exit("input already has cases; start from the plain map")
    sets = [int(h, 16) for h in data["sets"]]
    tests = list(data["tests"])
    n_real = len(tests)

    def unpack(table):
        return {f: {line: sets[k] for line, k in bucket.items()} for f, bucket in table.items()}

    func, mod, entry = unpack(data["func"]), unpack(data["mod"]), unpack(data["entry"])
    flat = {name: {f: sets[k] for f, k in data[name].items()} for name in ("opens", "lists", "imports", "inst")}
    vcpu = list(data["vcpu"])
    cost = list(data["cost"])
    wall = list(data["wall"])
    records = []
    for path in sorted(glob.glob(os.path.join(trace_dir, "*.json"))):
        if path.endswith(".cpu.json"):
            continue
        rec = json.load(open(path))
        cpu_path = path[:-5] + ".cpu.json"
        rec["measured"] = json.load(open(cpu_path))["cpu"] if os.path.exists(cpu_path) else None
        records.append(rec)
    records.sort(key=lambda r: (r["test"], r["fixture"] or "", r["ordinal"]))
    # Cases traced from a warm zygote (tools/trace_cases.py --warm) did not see the
    # imports: add the cold zero-case record's lines and entries, and give them the
    # whole test's instantiations, opened files and listed directories (a superset).
    outside_of = {r["test"]: r for r in records if r["fixture"] is None}
    real_index = {node: i for i, node in enumerate(tests)}
    for rec in records:
        if not rec.get("warm"):
            continue
        cold = outside_of.get(rec["test"])
        if cold is None:
            sys.exit(f"{rec['pseudo']}: warm case without a cold outside record")
        for key in ("func_lines", "mod_lines", "entered"):
            for f, items in cold[key].items():
                rec[key][f] = sorted(set(rec[key].get(f, [])) | set(items))
        # The CPU measured warm leaves out the imports; restricted_estimate models
        # every case as measured alone and cold, so add the cold zero-case run (an
        # overestimate by the warm part of the code outside the loops).
        if rec["measured"] is not None:
            rec["measured"] = None if cold["measured"] is None else rec["measured"] + cold["measured"]
        bit = 1 << real_index[rec["test"]]
        rec["opens"] = sorted(set(rec["opens"]) | set(cold["opens"]) | {f for f, m in flat["opens"].items() if m & bit})
        rec["lists"] = sorted(set(rec["lists"]) | set(cold["lists"]) | {f for f, m in flat["lists"].items() if m & bit})
        rec["inst"] = sorted(set(rec.get("inst", [])) | set(cold.get("inst", [])) | {f for f, m in flat["inst"].items() if m & bit})
    cases = {}
    missing_cpu = 0
    for rec in records:
        if rec["status"].get("failed"):
            sys.exit(f"case failed in a clean run: {rec['pseudo']}")
        i = len(tests)
        tests.append(rec["pseudo"])
        bit = 1 << i
        for f, lines in rec["func_lines"].items():
            b = func.setdefault(f, {})
            for line in lines:
                b[str(line)] = b.get(str(line), 0) | bit
        for f, lines in rec["mod_lines"].items():
            b = mod.setdefault(f, {})
            for line in lines:
                b[str(line)] = b.get(str(line), 0) | bit
            flat["imports"][f] = flat["imports"].get(f, 0) | bit
        for f, keys in rec["entered"].items():
            b = entry.setdefault(f, {})
            for key in keys:
                first = key.split(":", 1)[0]
                b[first] = b.get(first, 0) | bit
        for f in rec["opens"]:
            flat["opens"][f] = flat["opens"].get(f, 0) | bit
        for d in rec["lists"]:
            flat["lists"][d] = flat["lists"].get(d, 0) | bit
        for k in rec.get("inst", []):
            flat["inst"][k] = flat["inst"].get(k, 0) | bit
        if rec["measured"] is None:
            missing_cpu += 1
        vcpu.append(round(rec["measured"] / scale, 4) if rec["measured"] is not None else None)
        cost.append(rec["cpu"])
        wall.append(rec["status"].get("duration"))
        entry_ = cases.setdefault(rec["test"], {"outside": None, "fixtures": {}})
        if rec["fixture"] is None:
            entry_["outside"] = i
        else:
            entry_["fixtures"].setdefault(rec["fixture"], {})[rec["ordinal"]] = i
    for test, info in cases.items():
        if info["outside"] is None:
            sys.exit(f"{test}: no 'outside' record")
        for fixture, by_ordinal in info["fixtures"].items():
            n = max(by_ordinal) + 1
            if sorted(by_ordinal) != list(range(n)):
                sys.exit(f"{test} {fixture}: missing ordinals")
            info["fixtures"][fixture] = [by_ordinal[k] for k in range(n)]
    new_sets, index = [], {}

    def intern(mask):
        k = index.get(mask)
        if k is None:
            k = index[mask] = len(new_sets)
            new_sets.append(mask)
        return k

    def pack(table):
        return {f: {line: intern(m) for line, m in sorted(b.items(), key=lambda x: int(x[0]))} for f, b in sorted(table.items())}

    data.update({
        "tests": tests, "vcpu": vcpu, "cost": cost, "wall": wall, "real_tests": n_real,
        "func": pack(func), "mod": pack(mod), "entry": pack(entry),
        "opens": {f: intern(m) for f, m in sorted(flat["opens"].items())},
        "lists": {f: intern(m) for f, m in sorted(flat["lists"].items())},
        "imports": {f: intern(m) for f, m in sorted(flat["imports"].items())},
        "inst": {f: intern(m) for f, m in sorted(flat["inst"].items())},
        "cases": cases,
    })
    data["sets"] = [format(m, "x") for m in new_sets]
    with gzip.open(out, "wt", compresslevel=9) as handle:
        json.dump(data, handle, separators=(",", ":"))
    print(f"{n_real} tests + {len(records)} pseudo-tests for {len(cases)} tests; missing cpu {missing_cpu}; "
          f"sets {len(new_sets)}; size {os.path.getsize(out)}", file=sys.stderr)


if __name__ == "__main__":
    main()
