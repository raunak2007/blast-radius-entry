"""Re-score the current selector on mutants whose full failing sets are known.

work/mutants/waste.jsonl (from tools/waste.py) holds every failing test of each mutant.
This reruns only the selector on those patches and reports the new board cost, and any
mutant whose failing tests are no longer all selected (a miss on the real board).

Usage (from the blast-radius folder):  python tools/rescore.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from mutate import OUT, PRISTINE  # noqa: E402
from waste import board_cost  # noqa: E402
from br.impact import Analyzer  # noqa: E402
from br.importcheck import ImportChecker  # noqa: E402
from br.schedule import split  # noqa: E402
from selector import TpcdsProbe, Verifier, collection_modules, load_maps, select  # noqa: E402

MISS_SECONDS = 300.0
VERBOSE = "-v" in sys.argv


def main() -> None:
    tests = json.loads((ROOT / "sample" / "tests.json").read_text())["tests"]
    seconds = {t["number"]: t["seconds"] for t in tests}
    by_file: dict[str, set[int]] = defaultdict(set)
    for t in tests:
        by_file[t["file"]].add(t["number"])
    ms = {n: int(s * 1000) for n, s in seconds.items()}
    analyzer = Analyzer(str(PRISTINE), load_maps(tests))
    modules = collection_modules()
    checker = ImportChecker(str(PRISTINE), modules, analyzer.maps.import_groups.get("dependents", {}))
    tpcds = None if "--no-probe" in sys.argv else TpcdsProbe(tests, str(PRISTINE))
    verifier = None if "--no-verify" in sys.argv else Verifier(tests, checker)
    totals: dict[str, list[float]] = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    misses = []
    for line in (OUT / "waste.jsonl").open():
        row = json.loads(line)
        chosen = select(analyzer, (OUT / f"{row['id']}.patch").read_text(), set(seconds), modules, checker, tpcds, verifier)
        cost = board_cost(split(sorted(chosen), ms), seconds)
        # an unparsed entry is a whole file that failed to import: all its tests fail
        failing = set(row["failing"]).union(*(by_file.get(f, {-1}) for f in set(row["unparsed"])))
        if not failing <= chosen:
            misses.append((row["id"], sorted(failing - chosen)[:10]))
            cost += MISS_SECONDS
        total = totals[row["kind"]]
        total[0] += 1
        total[1] += row["ours"]
        total[2] += cost
        total[3] += row["oracle"]
        if VERBOSE and cost - row["oracle"] > 3:
            big = sorted((n for n in chosen if seconds[n] >= 1), key=lambda n: -seconds[n])
            print(f"{row['id']} {row['file']}: v7 {row['ours']:.1f} now {cost:.1f} oracle {row['oracle']:.1f} "
                  f"n={len(chosen)} big={[(n, seconds[n]) for n in big[:4]]}")
    checker.close()
    for kind, (count, before, after, oracle) in sorted(totals.items()):
        print(f"{kind:10} n={count:3d} before={before:7.1f} now={after:7.1f} oracle={oracle:7.1f}")
    print("MISSES:", misses)


if __name__ == "__main__":
    main()
