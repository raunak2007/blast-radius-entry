"""Build maps/coverage.json and maps/fixtures.json from offline runs at the pinned commit.

Inputs:
  argv[1]  sqlglot checkout (with .coverage from `pytest --cov-context=test`)
  argv[2]  tests.json (numbering)
  argv[3]  directory holding fixture-trace files fx.*.json
  argv[4]  output directory (maps/)

coverage.json: {"<file>": {"<line>": [test numbers...]}} for every line some test executed.
fixtures.json: {"<fixture path>": [test numbers...]}
"""

import glob
import gzip
import json
import os
import sys
from collections import defaultdict

from coverage import CoverageData


def context_to_tests(context: str, number_of: dict[str, int], groups: dict[str, set[int]]) -> set[int]:
    """Tests a coverage/trace context belongs to.

    "<nodeid>|run"            -> that test
    "<nodeid>|setup|teardown" -> every test in its class (setUpClass runs once per worker)
    "collect:<file>"          -> every test in that file (module import)
    """
    if context.startswith("collect:"):
        return groups.get(context[len("collect:"):], set())
    nodeid, _, phase = context.rpartition("|")
    if not nodeid:
        nodeid, phase = context, "run"
    if phase == "run":
        return {number_of[nodeid]} if nodeid in number_of else set()
    return groups.get(nodeid.rsplit("::", 1)[0], set()) | ({number_of[nodeid]} if nodeid in number_of else set())


def build_groups(number_of: dict[str, int]) -> dict[str, set[int]]:
    """Map every file and class prefix of a node id to the tests under it."""
    groups: dict[str, set[int]] = defaultdict(set)
    for nodeid, number in number_of.items():
        parts = nodeid.split("::")
        for depth in range(1, len(parts)):
            groups["::".join(parts[:depth])].add(number)
    return groups


def build_coverage(repo: str, number_of: dict[str, int]) -> dict:
    data = CoverageData(basename=os.path.join(repo, ".coverage"))
    data.read()
    groups = build_groups(number_of)
    result: dict[str, dict[str, list[int]]] = {}
    unknown: set[str] = set()
    for path in data.measured_files():
        rel = os.path.relpath(path, repo).replace(os.sep, "/")
        by_line = data.contexts_by_lineno(path)
        lines: dict[str, list[int]] = {}
        for line, contexts in by_line.items():
            nums: set[int] = set()
            for ctx in contexts:
                if not ctx:
                    continue
                got = context_to_tests(ctx, number_of, groups)
                if not got:
                    unknown.add(ctx)
                nums |= got
            if nums:
                lines[str(line)] = sorted(nums)
        if lines:
            result[rel] = lines
    print(f"coverage: {len(result)} files, unknown contexts {len(unknown)}", file=sys.stderr)
    return result


def build_fixtures(trace_dir: str, number_of: dict[str, int]) -> dict:
    return _invert(glob.glob(os.path.join(trace_dir, "fx.*.json")), number_of)


def build_classes(trace_dir: str, number_of: dict[str, int]) -> dict:
    return _invert(glob.glob(os.path.join(trace_dir, "fx-classes.*.json")), number_of)


def _invert(paths: list[str], number_of: dict[str, int]) -> dict:
    """{context: [keys]} trace files -> {key: [test numbers]}."""
    groups = build_groups(number_of)
    inverse: dict[str, set[int]] = defaultdict(set)
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for context, files in json.load(handle).items():
                tests = context_to_tests(context, number_of, groups)
                for f in files:
                    inverse[f] |= tests
    return {k: sorted(v) for k, v in sorted(inverse.items())}


def main() -> None:
    repo, tests_json, trace_dir, out_dir = sys.argv[1:5]
    with open(tests_json, encoding="utf-8") as handle:
        tests = json.load(handle)["tests"]
    number_of = {t["id"]: t["number"] for t in tests}
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "coverage.json"), "w", encoding="utf-8") as handle:
        json.dump(build_coverage(repo, number_of), handle, separators=(",", ":"))
    with open(os.path.join(out_dir, "fixtures.json"), "w", encoding="utf-8") as handle:
        json.dump(build_fixtures(trace_dir, number_of), handle, indent=1)
    with gzip.open(os.path.join(out_dir, "classes.json.gz"), "wt", encoding="utf-8") as handle:
        json.dump(build_classes(trace_dir, number_of), handle, separators=(",", ":"))


if __name__ == "__main__":
    main()
