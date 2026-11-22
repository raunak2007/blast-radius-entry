"""Find the tests that loop over SQL fixture cases (build-time tool).

Scans the pinned commit's test sources (AST only) for test methods that read a
fixture through tests.helpers.load_sql_fixture_pairs / load_sql_fixtures with a
constant file name, call TestOptimizer.check_file("<name>", ...) (which loops
over optimizer/<name>.sql), or iterate a class attribute that setUpClass filled
from such a loader (TestExecutor.tpch_sqls / tpcds_sqls). Every loop found this
way was read by hand to confirm its cases are independent (see selector/cases.py).

Usage: python tools/build_cases.py <sqlglot checkout> <tests.json> <out cases.json>
"""

import ast
import json
import os
import sys

LOADERS = {"load_sql_fixture_pairs": "pairs", "load_sql_fixtures": "lines"}


def call_name(node):
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def main():
    repo, tests_json, out = sys.argv[1:4]
    tests = {t["id"] for t in json.load(open(tests_json))["tests"]}
    result = {}
    also = {}
    for dirpath, dirnames, names in os.walk(os.path.join(repo, "tests")):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "fixtures")]
        for name in sorted(names):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), repo)
            tree = ast.parse(open(os.path.join(dirpath, name), encoding="utf-8").read())
            for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
                methods = {m.name: m for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
                # class attributes filled from a loader in setUpClass
                attr_fixture = {}
                setup = methods.get("setUpClass")
                if setup:
                    for node in ast.walk(setup):
                        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Attribute):
                            for call in ast.walk(node.value):
                                if isinstance(call, ast.Call) and call_name(call) in LOADERS and call.args and const_str(call.args[0]):
                                    attr_fixture[node.targets[0].attr] = (const_str(call.args[0]), LOADERS[call_name(call)])
                has_check_file = "check_file" in methods
                for mname, method in methods.items():
                    if not mname.startswith("test"):
                        continue
                    node_id = f"{rel}::{cls.name}::{mname}"
                    if node_id not in tests:
                        continue
                    found = []
                    for node in ast.walk(method):
                        if isinstance(node, ast.Call):
                            name_ = call_name(node)
                            if name_ in LOADERS and node.args and const_str(node.args[0]):
                                found.append((const_str(node.args[0]), LOADERS[name_]))
                            elif name_ == "check_file" and has_check_file and node.args and const_str(node.args[0]):
                                found.append((f"optimizer/{const_str(node.args[0])}.sql", "pairs"))
                        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self" \
                                and node.attr in attr_fixture:
                            found.append(attr_fixture[node.attr])
                    if found:
                        result[node_id] = sorted({tuple(f) for f in found})
                        # fixtures setUpClass loads for the class that this test never
                        # touches (TestExecutor loads TPC-H and TPC-DS for every test;
                        # each TPC test reads only its own list, also in subtestHelper,
                        # which picks the list by its `tpch` flag: checked by hand)
                        unused = {fx for fx in attr_fixture.values()} - set(result[node_id])
                        if unused:
                            also[node_id] = sorted(fx[0] for fx in unused)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from selector import cases as cases_mod

    counts = {}
    for fixtures in result.values():
        for fixture, kind in fixtures:
            text = open(os.path.join(repo, "tests", "fixtures", fixture), "rb").read().decode("utf-8")
            counts[fixture] = len(cases_mod.pair_cases(text) if kind == "pairs" else cases_mod.line_cases(text))
    json.dump({"tests": {k: [list(x) for x in v] for k, v in sorted(result.items())},
               "counts": dict(sorted(counts.items())), "also": dict(sorted(also.items()))}, open(out, "w"), indent=1)
    for k, v in sorted(result.items()):
        print(k, v)
    print(len(result), "tests", file=sys.stderr)


if __name__ == "__main__":
    main()
