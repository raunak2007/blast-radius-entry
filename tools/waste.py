"""Measure where the selector overspends, on the saved mutant patches.

For each work/mutants/<id>.patch it asks the selector for a worker split, applies the
patch to the scratch copy, runs only the selected tests, and compares the board cost of
our split with the oracle (exactly the failing tests, best packing). Unselected tests
were already run by tools/mutate.py, so the failing set here is complete.

Usage (from the blast-radius folder):
  python tools/waste.py [--limit N]
Appends one line per mutant to work/mutants/waste.jsonl and skips ids already there.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# BR_SELECTOR_ROOT: take the selection from another checkout (an older, broader
# version), so the failing set stays complete for mutants tools/mutate.py checked
SELECTOR_ROOT = Path(os.environ.get("BR_SELECTOR_ROOT", ROOT))
sys.path.insert(0, str(SELECTOR_ROOT))

# selector modules first, so mutate's own sys.path insert cannot swap them out
from br.diff import apply_hunks, parse_patch  # noqa: E402
from br.impact import Analyzer  # noqa: E402
from br.importcheck import ImportChecker  # noqa: E402
from br.schedule import split  # noqa: E402
from selector import collection_modules, load_maps, select  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
from mutate import OUT, PRISTINE, SCRATCH, run_unselected  # noqa: E402

WORKER_SECONDS = 2.0


def board_cost(workers: list[list[int]], seconds: dict[int, float]) -> float:
    if not workers:
        return 0.0
    return max(sum(seconds[n] for n in w) for w in workers) + WORKER_SECONDS * len(workers)


def apply_patch(patch_text: str) -> dict[Path, str]:
    """Write the patched files into the scratch copy; returns their original texts."""
    originals: dict[Path, str] = {}
    for fd in parse_patch(patch_text):
        target = SCRATCH / fd.path
        old = target.read_text(encoding="utf-8")
        originals[target] = old
        target.write_text("\n".join(apply_hunks(old.split("\n"), fd.hunks)), encoding="utf-8")
    return originals


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10_000)
    args = parser.parse_args()
    tests = json.loads((ROOT / "sample" / "tests.json").read_text())["tests"]
    id_of = {t["number"]: t["id"] for t in tests}
    num_of = {t["id"]: t["number"] for t in tests}
    seconds = {t["number"]: t["seconds"] for t in tests}
    ms = {n: int(s * 1000) for n, s in seconds.items()}
    analyzer = Analyzer(str(PRISTINE), load_maps(tests))
    modules = collection_modules()
    checker = ImportChecker(str(PRISTINE), modules, analyzer.maps.import_groups.get("dependents", {}))
    out = OUT / "waste.jsonl"
    done = {json.loads(l)["id"] for l in out.open()} if out.exists() else set()
    original = {json.loads(l)["id"]: json.loads(l) for l in (OUT / "results.jsonl").open()}
    kinds = {mid: row["kind"] for mid, row in original.items()}
    for patch in sorted(OUT.glob("*.patch"))[: args.limit]:
        mid = patch.stem
        if mid in done or mid not in kinds:
            continue
        chosen = select(analyzer, patch.read_text(), set(id_of), modules, checker)
        ours = split(sorted(chosen), ms)
        # tools/mutate.py ran only what its selector (maybe a broader version) left out:
        # if this selection is smaller, some tests were never run, so run them all
        to_run = chosen if len(chosen) >= original[mid]["selected"] else set(id_of)
        originals = apply_patch(patch.read_text())
        try:
            failed_ids = run_unselected([id_of[n] for n in sorted(to_run)])
        finally:
            for target, text in originals.items():
                target.write_text(text, encoding="utf-8")
        failing = sorted({num_of[i] for i in failed_ids if i in num_of})
        record = {
            "id": mid, "kind": kinds[mid], "file": patch.read_text().split("\n", 1)[0].split(" b/")[-1],
            "selected": len(chosen), "failing": failing, "unparsed": [i for i in failed_ids if i not in num_of],
            "ours": round(board_cost(ours, seconds), 3),
            "oracle": round(board_cost(split(failing, ms) if failing else [], seconds), 3),
            "big": [n for n in chosen if seconds[n] >= 3],
        }
        with out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        print(json.dumps(record), flush=True)


if __name__ == "__main__":
    main()
