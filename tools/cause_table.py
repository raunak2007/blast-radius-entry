"""Excess cost by cause, summed over sets, for one or more runs (dev tool).

Usage: python tools/cause_table.py <tag> [<tag> ...]   (reads .work/out/<set><tag>.{jsonl,plans})
"""

import json
import os
import subprocess
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETS = {
    "sample": (os.path.join(HERE, ".work/data/sample"), os.path.join(HERE, "../_kits/blast-radius/sample/sample/answers.json")),
    "val1": (os.path.join(HERE, ".work/data/val1"), os.path.join(HERE, "validation/val1/answers.json")),
    "val2": (os.path.join(HERE, ".work/data/val2"), os.path.join(HERE, "validation/val2/answers.json")),
    "val4": (os.path.join(HERE, ".work/data/val4"), os.path.join(HERE, "validation/val4/answers.json")),
    "val3": (os.path.join(HERE, ".work/data/val3"), os.path.join(HERE, "validation/val3/answers.json")),
    "val5": (os.path.join(HERE, ".work/data/val5"), os.path.join(HERE, "validation/val5/answers.json")),
}
GROUP = {
    "blind-exp": "expensive test included (blind)",
    "conditional-exp": "expensive test included (probe not inert)",
    "blind-imp": "cheap tests, import-time edit, included",
    "blind-func": "cheap tests, function edit, included",
    "blind-data": "cheap fixture readers included",
    "blind-pydata": "tests reading a .py as data included",
    "conditional-cheap": "cheap tests left for probe round 2",
    "unverified": "verification unavailable (killed/deadline)",
    "unknown-all": "unknown change: everything",
    "import-error": "import error fallback",
    "certain": "certain (module broken)",
}


def group_of(cause):
    for prefix, name in GROUP.items():
        if cause.startswith(prefix):
            return name
    return cause


def main():
    tags = sys.argv[1:]
    table = defaultdict(lambda: defaultdict(float))
    totals = defaultdict(lambda: defaultdict(float))
    for tag in tags:
        for s, (data, answers) in SETS.items():
            out = os.path.join(HERE, f".work/out/{s}{tag}")
            if not os.path.exists(out + ".jsonl"):
                continue
            js = out + ".bd.json"
            subprocess.run([sys.executable, os.path.join(HERE, "tools/breakdown.py"), data, answers, out + ".jsonl",
                            "--plans", out + ".plans", "--rows", "0", "--json", js], check=True, capture_output=True)
            bd = json.load(open(js))
            for cause, (ms, _) in bd["by_cause"].items():
                table[group_of(cause)][(tag, s)] += ms / 1000
            for r in bd["rows"]:
                totals[tag][s] += r["excess"] / 1000
            plans = [json.loads(l) for l in open(out + ".plans")]
            totals[tag][s + ":cpu"] = sum(p.get("verify_cpu") or 0 for p in plans)
    sets = [s for s in SETS]
    for tag in tags:
        print(f"== {tag}: excess over oracle by cause (s); " + ", ".join(
            f"{s} {totals[tag][s]:.1f} (verify cpu {totals[tag][s + ':cpu']:.1f})" for s in sets if s in totals[tag]))
        rows = sorted(table.items(), key=lambda kv: -sum(v for (t, _), v in kv[1].items() if t == tag))
        print(f"  {'cause':48} " + " ".join(f"{s:>7}" for s in sets) + "   total")
        for cause, cells in rows:
            vals = [cells.get((tag, s), 0.0) for s in sets]
            if sum(vals) < 0.05:
                continue
            print(f"  {cause:48} " + " ".join(f"{v:7.1f}" for v in vals) + f" {sum(vals):7.1f}")


if __name__ == "__main__":
    main()
