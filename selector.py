"""Blast Radius selector: pick the tests each sqlglot patch can break.

Reads /data/tests.json and /data/commits/*.patch, prints one JSON line per commit.
Coverage and fixture maps were built offline from sqlglot at the pinned commit
(see tools/). Any error on a commit falls back to running every test.
"""

from __future__ import annotations

import gzip
import json
import os
import pickle
import resource
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from br.diff import parse_patch  # noqa: E402
from br.impact import ALL, Analyzer, Maps  # noqa: E402
from br.importcheck import ImportChecker  # noqa: E402
from br import tpcfix  # noqa: E402
from br import probe  # noqa: E402
from br.schedule import split  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = os.environ.get("BR_SQLGLOT", "/opt/sqlglot")
MAPS = Path(os.environ.get("BR_MAPS", str(HERE / "maps")))


def log(message: str) -> None:
    print(message, file=sys.stderr)


def _load(name: str):
    """Load maps/<name> (.json or .json.gz), preferring a .pickle made at image build."""
    stem = name.removesuffix(".gz").removesuffix(".json")
    pickled = MAPS / f"{stem}.pickle"
    if pickled.exists():
        with open(pickled, "rb") as handle:
            return pickle.load(handle)
    path = MAPS / name
    if not path.exists():
        return None
    opener = gzip.open if name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def load_maps(tests: list[dict]) -> Maps:
    raw = _load("coverage.json.gz")
    if (MAPS / "coverage.pickle").exists():
        coverage = raw  # already int-keyed
    else:
        coverage = {path: {int(k): v for k, v in lines.items()} for path, lines in raw.items()}
    by_file: dict[str, set[int]] = defaultdict(set)
    for test in tests:
        by_file[test["file"]].add(test["number"])
    return Maps(
        coverage=coverage,
        fixtures=_load("fixtures.json") or {},
        tests_by_file=dict(by_file),
        classes=_load("classes.json.gz") or {},
        enums=frozenset(_load("enums.json") or ()),
        import_lines=_load("import_lines.json") or {},
        import_groups=_load("import_groups.json.gz") or {},
        collect_imports=_load("collect_imports.json") or {},
    )


REFINE_MIN_SECONDS = 3.0  # board cost below which the import-state diff cannot pay for itself
MAX_PROBE_QUERIES = 15  # board CPU ~2.2 s + 0.73 s per query against 21.5 s saved when it passes
VERIFY_MAX_SECONDS = 3.0  # recorded seconds of a selection we run ourselves to keep only its failures
VERIFY_TIMEOUT_FACTOR = 5.0
VERIFY_CPU_FACTOR = 1.2  # board CPU per recorded test second (the board machine is ~2.2x slower than dev)
VERIFY_FIXED_SECONDS = 0.25  # fork and a fresh import of sqlglot, on the board
VERIFY_WORTH = 0.5  # run when the estimate is below this share of the selection's board cost
# setUpClass work a run pays once per test class (loading TPC tables, DuckDB, pandas)
SETUP_SECONDS = {"tests/test_executor.py::TestExecutor": 2.2, "tests/test_optimizer.py::TestOptimizer": 0.7}
WORKER_SECONDS = 2.0


class Verifier:
    """Runs a small selection on the patched sources and keeps only the tests that fail.

    Worth it when the selection is cheap: if nothing fails the commit costs 0 instead of
    2 s for a worker plus the tests. The run happens in a fresh import of everything
    (no state from the warm process), with process pools run inline and subprocesses
    refused, so a test that would see unpatched code errors out and stays selected.
    """

    def __init__(self, tests: list[dict], checker: ImportChecker) -> None:
        self.ids = {t["number"]: t["id"] for t in tests}
        self.seconds = {t["number"]: t["seconds"] for t in tests}
        self.numbers = {t["id"]: t["number"] for t in tests}
        self.checker = checker

    def board_cost(self, chosen: set[int]) -> float:
        workers = split(chosen, {n: round(self.seconds[n] * 1000) for n in chosen})
        return max((sum(self.seconds[n] for n in w) for w in workers), default=0.0) + WORKER_SECONDS * len(workers)

    def estimate(self, chosen: set[int]) -> float:
        classes = {self.ids[n].rsplit("::", 1)[0] for n in chosen}
        setup = sum(SETUP_SECONDS.get(c, 0.0) for c in classes)
        return VERIFY_CPU_FACTOR * sum(self.seconds[n] for n in chosen) + setup + VERIFY_FIXED_SECONDS

    def verify(self, analyzer: Analyzer, chosen: set[int]) -> set[int]:
        cost = sum(self.seconds[n] for n in chosen)
        if not chosen or cost > VERIFY_MAX_SECONDS or not analyzer.verifiable:
            return chosen
        if self.estimate(chosen) > VERIFY_WORTH * self.board_cost(chosen):
            return chosen
        failed = self.checker.run_tests(
            analyzer.patched_sources, [self.ids[n] for n in sorted(chosen)], VERIFY_TIMEOUT_FACTOR * cost + 5
        )
        if failed is None or not failed <= set(self.numbers):
            log("  verify: run did not finish cleanly, keeping the selection")
            return chosen
        kept = {self.numbers[i] for i in failed} & chosen
        log(f"  verify: {len(kept)} of {len(chosen)} selected tests fail")
        return kept


class TpcdsProbe:
    """Decides test_execute_tpcds by running only the queries a function edit can reach."""

    def __init__(self, tests: list[dict], src_root: str) -> None:
        raw = _load("tpcds_queries.json.gz") or {}
        self.lines: dict[str, dict] = raw.get("lines", {})
        self.src_root = src_root
        self.number = next((t["number"] for t in tests if t["id"] == probe.TEST_ID), None)

    def queries(self, ranges: list[tuple[str, int, int]]) -> set[int]:
        out: set[int] = set()
        for path, start, end in ranges:
            lines = self.lines.get(path, {})
            for line in range(start, end + 1):
                out.update(lines.get(line, lines.get(str(line), ())))
        return out

    def prune(self, analyzer: Analyzer, chosen: set[int]) -> set[int]:
        if self.number in chosen and analyzer.fixture_probe and not analyzer.patched_sources:
            return self.prune_fixture(analyzer, chosen)
        if self.number not in chosen or not self.lines or analyzer.import_time:
            return chosen
        ranges = analyzer.changed_ranges
        if not ranges:
            return chosen
        queries = self.queries(ranges)
        if not queries or len(queries) > MAX_PROBE_QUERIES:
            log(f"  tpcds probe skipped: {len(queries)} queries")
            return chosen
        passed = probe.run_queries(self.src_root, analyzer.patched_sources, sorted(queries))
        log(f"  tpcds probe on {len(queries)} queries: {'pass, dropping it' if passed else 'fail, keeping it'}")
        return chosen - {self.number} if passed else chosen


    def prune_fixture(self, analyzer: Analyzer, chosen: set[int]) -> set[int]:
        """A TPC-DS fixture edit (no Python change): replay only the queries whose sql or meta changed."""
        if set(analyzer.fixture_probe) != {tpcfix.TPCDS}:
            return chosen
        text, indices = analyzer.fixture_probe[tpcfix.TPCDS]
        if not indices or len(indices) > MAX_PROBE_QUERIES:
            return chosen
        rel = tpcfix.TPCDS[len(tpcfix.FIXTURES):]
        passed = probe.run_queries(self.src_root, {"__fixtures__": {rel: text}}, indices)
        log(f"  tpcds fixture probe on {len(indices)} queries: {'pass, dropping it' if passed else 'fail, keeping it'}")
        return chosen - {self.number} if passed else chosen


def select(analyzer: Analyzer, patch_text: str, everything: set[int], modules: list[str],
           checker: ImportChecker, tpcds: TpcdsProbe | None = None, verifier: Verifier | None = None) -> set[int]:
    worth = (lambda tests: verifier.board_cost(tests) > REFINE_MIN_SECONDS) if verifier is not None else None
    chosen = _select(analyzer, patch_text, everything, modules, checker, worth)
    if tpcds is not None and chosen != everything:
        chosen = tpcds.prune(analyzer, chosen)
    if verifier is not None and chosen != everything:
        chosen = verifier.verify(analyzer, chosen)
    return chosen


def _select(analyzer: Analyzer, patch_text: str, everything: set[int], modules: list[str],
            checker: ImportChecker, worth_refining=None) -> set[int]:
    analyzer.reset()
    per_file: list[tuple[str, set[int] | None]] = []
    diffs = parse_patch(patch_text)
    # running tests here sees only patched .py modules: no fixture, data or file renames
    analyzer.verifiable = bool(diffs) and all(
        fd.path.endswith(".py") and fd.old_path == fd.new_path and not fd.binary for fd in diffs
    )
    for file_diff in diffs:
        per_file.append((file_diff.path, analyzer.tests_for(file_diff)))
    analyzer.verifiable = analyzer.verifiable and all(fd.path in analyzer.patched_sources for fd in diffs)
    conservative = _union(got for _, got in per_file)
    if not analyzer.import_time:
        return set(everything) if conservative is ALL else conservative & everything
    # the state snapshot costs ~0.25 s of board CPU: skip it when the selection is already cheap
    state = conservative is ALL or worth_refining is None or worth_refining(conservative & everything)
    crashed, changed = checker.check(analyzer.import_time, state=state)
    if crashed:
        return _crash_selection(analyzer, per_file, everything, modules, checker)
    refined = analyzer.refine(changed)
    others = _union(got for path, got in per_file if path not in analyzer.refine_info)
    if refined is not None and others is not ALL:
        log(f"  import-time state changed: {sorted(changed)[:6]}")
        return (refined | others) & everything
    return set(everything) if conservative is ALL else conservative & everything


def _crash_selection(analyzer: Analyzer, per_file, everything: set[int], modules: list[str],
                     checker: ImportChecker) -> set[int]:
    """An import-time crash: the tests that load a module that now fails to import, plus the
    selections of patched files that still import (their edits stay visible)."""
    names = [m for m in modules if not m.startswith("tests")]
    failed = checker.failing_modules(analyzer.import_time, names)
    gone = {path for path in analyzer.import_time if _module_name(path) in (failed or ())}
    # Only when every patched module with import-time edits itself fails to import is the
    # crash the patch's whole effect; a patched module that still imports can change state
    # its importers read in ways no coverage shows (a lazy __getattr__ table, say).
    crash = analyzer.crash_tests(failed) if failed and gone == set(analyzer.import_time) else ALL
    if crash is ALL:
        log("  import-time crash detected: selecting every test")
        return set(everything)
    others = _union(got for path, got in per_file if path not in gone)
    if others is ALL:
        log("  import-time crash, other files unbounded: selecting every test")
        return set(everything)
    log(f"  import-time crash in {failed[:6]}: {len(crash)} tests load them")
    return (crash | others) & everything


def _module_name(path: str) -> str:
    name = path[:-3].replace("/", ".")
    return name[: -len(".__init__")] if name.endswith(".__init__") else name


def _union(parts) -> set[int] | None:
    out: set[int] = set()
    for part in parts:
        if part is ALL:
            return ALL
        out |= part
    return out


def collection_modules() -> list[str]:
    """Module names test files import at collection (from maps/collect_imports.json)."""
    path = MAPS / "collect_imports.json"
    if not path.exists():
        return []
    names = []
    for rel in json.loads(path.read_text()):
        name = rel[:-3].replace("/", ".")
        names.append(name[: -len(".__init__")] if name.endswith(".__init__") else name)
    return names


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms_of = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    everything = set(ms_of)
    analyzer = Analyzer(SRC, load_maps(tests))
    analyzer.test_numbers = {t["id"]: t["number"] for t in tests}
    modules = collection_modules()
    checker = ImportChecker(SRC, modules, analyzer.maps.import_groups.get("dependents", {}))
    tpcds = None if os.environ.get("BR_NO_PROBE") else TpcdsProbe(tests, SRC)
    verifier = None if os.environ.get("BR_NO_VERIFY") else Verifier(tests, checker)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            chosen = select(analyzer, patch.read_text(encoding="utf-8", errors="replace"), everything, modules,
                            checker, tpcds, verifier)
        except Exception as error:  # noqa: BLE001 - a crash must never cost a miss
            log(f"{patch.stem}: fallback to all tests ({type(error).__name__}: {error})")
            chosen = set(everything)
        print(json.dumps({"commit": patch.stem, "workers": split(chosen, ms_of)}))
        log(f"{patch.stem}: {len(chosen)} tests")
    checker.close()
    sys.stdout.flush()
    usage = [resource.getrusage(who) for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
    log(f"cpu seconds: {sum(u.ru_utime + u.ru_stime for u in usage):.2f}")


if __name__ == "__main__":
    main()
