"""Per-commit cost breakdown of a selector output against answers (dev tool).

Usage: python tools/breakdown.py <data dir> <answers.json> <output.jsonl> [--plans plans.jsonl]
                                 [--meta meta.json] [--rows N] [--json out.json]

With --plans (written by the selector when BR_PLAN_DUMP is set) every
over-selected test is attributed to the path that put it in the selection
(blind inclusion by reason, expensive test included blindly, probe that did
not prove the edit inert, verification unavailable, ...). The excess of a
commit (its cost minus the oracle cost of packing exactly its failing tests)
is split over those causes greedily: the cause whose removal saves the most
goes first, and each cause gets the saving its removal adds.
"""

import argparse
import json
import sys
from collections import defaultdict

sys.path.insert(0, __file__.rsplit("/tools/", 1)[0])
from selector.pack import pack  # noqa: E402


def cost_of(numbers, ms):
    return pack(sorted(numbers), ms)[1] if numbers else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir")
    ap.add_argument("answers")
    ap.add_argument("output")
    ap.add_argument("--plans")
    ap.add_argument("--meta")
    ap.add_argument("--rows", type=int, default=25)
    ap.add_argument("--json")
    a = ap.parse_args()
    tests = json.load(open(f"{a.data_dir}/tests.json"))["tests"]
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    name = {t["number"]: t["id"] for t in tests}
    answers = json.load(open(a.answers))["commits"]
    meta = json.load(open(a.meta)) if a.meta else {}
    plans = {}
    if a.plans:
        for line in open(a.plans):
            obj = json.loads(line)
            plans[obj["commit"]] = obj
    rows = []
    by_kind = defaultdict(lambda: [0, 0, 0, 0])
    by_cause = defaultdict(lambda: [0, 0])  # ms, commits
    by_cause_kind = defaultdict(lambda: defaultdict(int))
    total = oracle_total = misses = 0
    cpu_total = 0.0
    for line in open(a.output):
        obj = json.loads(line)
        cid = obj["commit"]
        if cid not in answers:
            continue
        workers = [w for w in obj["workers"] if w]
        selected = {n for w in workers for n in w}
        cost = max(sum(ms[n] for n in w) for w in workers) + 2000 * len(workers) if workers else 0
        failing = set(answers[cid]["failing"])
        miss = bool(failing - selected)
        oracle = cost_of(failing, ms)
        kind = answers[cid].get("kind") or meta.get(cid, {}).get("kind", "?")
        plan = plans.get(cid, {})
        why = {int(k): v for k, v in plan.get("why", {}).items()}
        cpu_total += plan.get("verify_cpu", 0.0) or 0.0
        extra = selected - failing
        causes = defaultdict(set)
        for n in extra:
            causes[why.get(n, "?")].add(n)
        # greedy attribution of the excess
        attributed = {}
        current = set(selected)
        remaining = dict(causes)
        while remaining:
            base = cost_of(current, ms)
            best = max(remaining, key=lambda c: (base - cost_of(current - remaining[c], ms), c))
            attributed[best] = base - cost_of(current - remaining[best], ms)
            current -= remaining.pop(best)
        for c, v in attributed.items():
            by_cause[c][0] += v
            by_cause[c][1] += 1
            by_cause_kind[c][kind] += v
        expensive_extra = sorted((n for n in extra if ms[n] >= 2000), key=lambda n: -ms[n])
        rows.append({
            "cid": cid, "kind": kind, "cost": cost, "oracle": oracle, "miss": miss,
            "excess": cost - oracle + (300000 if miss else 0), "selected": len(selected), "failing": len(failing),
            "causes": {c: v for c, v in sorted(attributed.items(), key=lambda x: -x[1]) if v},
            "expensive_extra": [name[n].split("::")[-1] for n in expensive_extra],
            "note": plan.get("note", "")[:160], "verify_cpu": plan.get("verify_cpu", 0.0),
        })
        total += cost + (300000 if miss else 0)
        oracle_total += oracle
        misses += miss
        k = by_kind[kind]
        k[0] += 1
        k[1] += cost + (300000 if miss else 0)
        k[2] += oracle
        k[3] += miss
    rows.sort(key=lambda r: -r["excess"])
    print(f"total {total / 1000:.1f}s  oracle {oracle_total / 1000:.1f}s  excess {(total - oracle_total) / 1000:.1f}s  "
          f"misses {misses}  commits {len(rows)}  verify-cpu(local) {cpu_total:.1f}s")
    print("by kind:")
    for kind, (n, c, o, m) in sorted(by_kind.items(), key=lambda x: -(x[1][1] - x[1][2])):
        print(f"  {kind:11} n={n:3} cost={c / 1000:8.1f} oracle={o / 1000:7.1f} excess={(c - o) / 1000:7.1f} misses={m}")
    if plans:
        print("excess by cause (greedy attribution):")
        for c, (v, n) in sorted(by_cause.items(), key=lambda x: -x[1][0]):
            kinds = ", ".join(f"{k}={s / 1000:.1f}" for k, s in sorted(by_cause_kind[c].items(), key=lambda x: -x[1]) if s)
            print(f"  {c:22} {v / 1000:8.1f}s in {n:3} commits  [{kinds}]")
    print("worst commits:")
    for r in rows[: a.rows]:
        causes = " ".join(f"{c}={v / 1000:.1f}" for c, v in r["causes"].items())
        print(f"  {r['cid']} {r['kind']:10} excess={r['excess'] / 1000:6.2f} cost={r['cost'] / 1000:6.2f} "
              f"oracle={r['oracle'] / 1000:6.2f} {'MISS ' if r['miss'] else ''}sel={r['selected']} fail={r['failing']} "
              f"cpu={r['verify_cpu']:.1f} | {causes} | {','.join(r['expensive_extra'])}")
    if a.json:
        json.dump({"rows": rows, "by_cause": dict(by_cause)}, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
