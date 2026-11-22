"""Write maps/import_groups.json.gz: which tests depend on each line that runs at import.

Code that runs while a module is imported (metaclasses building tries, class bodies,
module tables) shapes state every later test uses, but coverage credits it to no test
or to whichever test first triggered a lazy import. This script imports each sqlglot
module in a fresh interpreter and records the lines that ran:

  core group  : lines run by `import sqlglot`               -> every test ("ALL")
  module group: lines run by importing module M afterwards   -> tests that construct a
                class from the modules M loaded, whose test file imports them at
                collection, or that execute code in them

Run from the blast-radius folder after build_maps.py and collect_imports.py:
  python tools/import_groups.py work/sqlglot sample/tests.json maps
"""

import gzip
import json
import os
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

PROBE = r"""
import importlib, json, os, sys, coverage
target, root = sys.argv[1], sys.argv[2]
cov = coverage.Coverage(data_file=None, source=["sqlglot"])
before = set()
if target != "<core>":
    import sqlglot
    before = set(sys.modules)
cov.start()
if target == "<core>":
    import sqlglot
else:
    importlib.import_module(target)
cov.stop()
data = cov.get_data()
lines = {os.path.relpath(f, root): sorted(data.lines(f) or []) for f in data.measured_files()}
loaded = []
for name in set(sys.modules) - before:
    path = getattr(sys.modules[name], "__file__", None) or ""
    if name.startswith("sqlglot") and path.endswith(".py"):
        base = name.replace(".", "/")
        loaded.append(base + "/__init__.py" if path.endswith("__init__.py") else base + ".py")
print(json.dumps({"lines": lines, "loaded": sorted(loaded)}))
"""


def probe(repo: str, target: str) -> tuple[str, dict]:
    proc = subprocess.run(
        [sys.executable, "-c", PROBE, target, repo], cwd=repo, capture_output=True, text=True,
        env={"PYTHONPATH": repo, "PATH": "/usr/bin:/bin"}, timeout=300,
    )
    if proc.returncode != 0:
        print(f"{target}: failed {proc.stderr[-300:]}", file=sys.stderr)
        return target, {"lines": {}, "loaded": []}
    return target, json.loads(proc.stdout.strip().splitlines()[-1])


def module_names(repo: str) -> list[str]:
    names = []
    for folder, _, files in os.walk(os.path.join(repo, "sqlglot")):
        for name in files:
            if name.endswith(".py") and name != "__main__.py":
                rel = os.path.relpath(os.path.join(folder, name), repo)[:-3].replace(os.sep, ".")
                names.append(rel[: -len(".__init__")] if rel.endswith(".__init__") else rel)
    return sorted(names)


def group_tests(loaded: list[str], classes: dict, collect: dict, coverage: dict, by_file: dict) -> set[int]:
    tests: set[int] = set()
    prefixes = tuple(f"{m}::" for m in loaded)
    for key, users in classes.items():
        if key.startswith(prefixes):
            tests.update(users)
    for module in loaded:
        # collection importers matter only for import crashes, which the run-time import
        # check catches; non-crashing import-time state reaches class users and callers
        for line_tests in coverage.get(module, {}).values():
            tests.update(line_tests)
    return tests


def main() -> None:
    repo, tests_json, maps = os.path.abspath(sys.argv[1]), sys.argv[2], sys.argv[3]
    with open(tests_json, encoding="utf-8") as handle:
        tests = json.load(handle)["tests"]
    by_file: dict[str, set[int]] = defaultdict(set)
    for t in tests:
        by_file[t["file"]].add(t["number"])
    with gzip.open(os.path.join(maps, "classes.json.gz"), "rt", encoding="utf-8") as handle:
        classes = json.load(handle)
    with gzip.open(os.path.join(maps, "coverage.json.gz"), "rt", encoding="utf-8") as handle:
        coverage = json.load(handle)
    with open(os.path.join(maps, "collect_imports.json"), encoding="utf-8") as handle:
        collect = json.load(handle)

    targets = ["<core>"] + module_names(repo)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(pool.map(lambda t: probe(repo, t), targets))

    line_groups: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    groups: dict[int, object] = {}
    for gid, target in enumerate(targets):
        result = results[target]
        if not result["lines"]:
            continue
        groups[gid] = "ALL" if target == "<core>" else sorted(
            group_tests(result["loaded"], classes, collect, coverage, by_file)
        )
        for path, lines in result["lines"].items():
            for line in lines:
                line_groups[path][str(line)].append(gid)
    # dependents: module path -> module names whose import (after `import sqlglot`) loads it
    dependents: dict[str, set[str]] = defaultdict(set)
    for target in targets[1:]:
        for path in results[target]["loaded"]:
            dependents[path].add(target)
    out = {
        "lines": line_groups,
        "groups": {str(k): v for k, v in groups.items()},
        "dependents": {k: sorted(v) for k, v in dependents.items()},
    }
    with gzip.open(os.path.join(maps, "import_groups.json.gz"), "wt", encoding="utf-8") as handle:
        json.dump(out, handle, separators=(",", ":"))
    print(f"{len(groups)} groups, {sum(len(v) for v in line_groups.values())} lines", file=sys.stderr)


if __name__ == "__main__":
    main()
