"""Write maps/collect_imports.json: which test files import each sqlglot module at collection.

An import-time crash in module M (for example a class body that fails) breaks every
test in every test file whose import chain reaches M, even tests that never use M.
Each test file is imported in a fresh interpreter and the sqlglot modules it loaded
are recorded.

Run from the blast-radius folder:
  python tools/collect_imports.py work/sqlglot sample/tests.json maps/collect_imports.json
"""

import json
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

PROBE = """
import importlib, json, sys
importlib.import_module(sys.argv[1])
out = []
for name, module in list(sys.modules.items()):
    path = getattr(module, "__file__", None) or ""
    if name.startswith("sqlglot") and path.endswith(".py"):
        base = name.replace(".", "/")
        out.append(base + "/__init__.py" if path.endswith("__init__.py") else base + ".py")
print(json.dumps(sorted(out)))
"""


def modules_for(repo: str, test_file: str) -> tuple[str, list[str]]:
    module = test_file[:-3].replace("/", ".")
    proc = subprocess.run(
        [sys.executable, "-c", PROBE, module], cwd=repo, capture_output=True, text=True,
        env={"PYTHONPATH": repo, "SKIP_INTEGRATION": "0", "PATH": "/usr/bin:/bin"}, timeout=300,
    )
    if proc.returncode != 0:
        print(f"{test_file}: import failed\n{proc.stderr[-500:]}", file=sys.stderr)
        return test_file, []
    return test_file, json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> None:
    repo, tests_json, out = sys.argv[1:4]
    with open(tests_json, encoding="utf-8") as handle:
        files = sorted({t["file"] for t in json.load(handle)["tests"]})
    inverse: dict[str, set[str]] = defaultdict(set)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for test_file, modules in pool.map(lambda f: modules_for(repo, f), files):
            for module in modules:
                inverse[module].add(test_file)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump({k: sorted(v) for k, v in sorted(inverse.items())}, handle, indent=0)
    print(f"{len(files)} test files, {len(inverse)} modules", file=sys.stderr)


if __name__ == "__main__":
    main()
