"""Safety check for the TPC-DS shortcut: mutate code that TPC-DS queries run, then compare
the selector's decision (drop or keep test_execute_tpcds) with the real test's outcome.

The mutations are only used to test the selector; nothing here feeds the map.

usage: python validate_mutations.py <pristine sqlglot> <worktree> <selector.py> <map> <tests.json> <n> <seed> <out.json> [class-weight]
"""
import ast
import difflib
import gzip
import json
import os
import pickle
import random
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

pristine, work, selector, map_path, tests_path, n, seed, out_path, tpcds_path = sys.argv[1:10]
class_weight = 0.0
n, seed = int(n), int(seed)
rng = random.Random(seed)
VENV_PY = "/home/claude/rwx/.venv313/bin/python"

tests = json.load(open(tests_path))["tests"]
by_id = {t["id"]: t for t in tests}
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t["id"])
doc = pickle.load(gzip.open(map_path))
covered = doc["lines"]


def candidate_sites(rel, text):
    tree = ast.parse(text)
    lines = text.split("\n")
    sites = []

    def add(line, col, end_col, new, kind, node):
        old_line = lines[line - 1]
        old = old_line[col:end_col]
        if not old or old == new:
            return
        sites.append((line, col, end_col, old, new, kind))

    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and len(node.values) >= 2:
            a, b = node.values[0], node.values[1]
            if a.end_lineno == b.lineno:
                seg = lines[a.end_lineno - 1][a.end_col_offset:b.col_offset]
                op = "and" if isinstance(node.op, ast.And) else "or"
                k = seg.find(op)
                if k >= 0:
                    add(a.end_lineno, a.end_col_offset + k, a.end_col_offset + k + len(op), "or" if op == "and" else "and", "boolop", node)
        elif isinstance(node, ast.Compare) and node.ops:
            left, right = node.left, node.comparators[0]
            if left.end_lineno == right.lineno:
                seg = lines[left.end_lineno - 1][left.end_col_offset:right.col_offset]
                swaps = {ast.Eq: ("==", "!="), ast.NotEq: ("!=", "=="), ast.Lt: ("<", "<="), ast.LtE: ("<=", "<"),
                         ast.Gt: (">", ">="), ast.GtE: (">=", ">"), ast.Is: ("is", "=="), ast.In: ("in", "not in")}
                pair = swaps.get(type(node.ops[0]))
                if pair:
                    k = seg.find(pair[0])
                    if k >= 0:
                        add(left.end_lineno, left.end_col_offset + k, left.end_col_offset + k + len(pair[0]), pair[1], "compare", node)
        elif isinstance(node, ast.Constant) and node.lineno == node.end_lineno:
            if node.value is True:
                add(node.lineno, node.col_offset, node.end_col_offset, "False", "bool", node)
            elif node.value is False:
                add(node.lineno, node.col_offset, node.end_col_offset, "True", "bool", node)
            elif type(node.value) is int and 0 <= node.value < 1000:
                add(node.lineno, node.col_offset, node.end_col_offset, str(node.value + 1), "int", node)
            elif isinstance(node.value, str) and 1 <= len(node.value) <= 30 and node.value.isupper() and node.value.isalpha():
                src = lines[node.lineno - 1][node.col_offset:node.end_col_offset]
                if src in (f'"{node.value}"', f"'{node.value}'"):
                    add(node.lineno, node.col_offset, node.end_col_offset, src[0] + node.value[:-1] + "X" + src[0], "string", node)
    return sites


def scope_of(text, line):
    """'function' if the line sits inside a def body, else 'class'/'module'."""
    tree = ast.parse(text)
    best = "module"

    def walk(node, scope):
        nonlocal best
        for child in ast.iter_child_nodes(node):
            lo, hi = getattr(child, "lineno", None), getattr(child, "end_lineno", None)
            if lo is None or not (lo <= line <= hi):
                walk(child, scope)
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body_lo = child.body[0].lineno
                if line >= body_lo:
                    best = "function"
                    walk(child, "function")
                continue
            if isinstance(child, ast.ClassDef):
                if scope != "function":
                    best = "class"
                walk(child, "class" if scope != "function" else scope)
                continue
            walk(child, scope)

    walk(tree, "module")
    return best


def make_patch(rel, old_text, new_text):
    diff = difflib.unified_diff(old_text.split("\n"), new_text.split("\n"), f"a/{rel}", f"b/{rel}", lineterm="", n=3)
    body = "\n".join(diff)
    return f"diff --git a/{rel} b/{rel}\n{body}\n"


def failing_tests(junit):
    fails = set()
    root = ET.parse(junit).getroot()
    for case in root.iter("testcase"):
        if not any(k.tag in ("failure", "error") for k in case):
            continue
        cls, name = case.get("classname", ""), case.get("name", "")
        if "." in cls:
            mod, klass = cls.rsplit(".", 1)
            tid = f"{mod.replace('.', '/')}.py::{klass}::{name}"
            if tid in by_id:
                fails.add(tid)
                continue
            path = cls.replace(".", "/") + ".py"
            if path in by_file:  # collection error for a whole file
                fails.update(by_file[path])
                continue
        fails.add(f"?{cls}::{name}")
    return fails



TPCDS_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
tpc = pickle.load(gzip.open(tpcds_path))
setup = {r: set(v) for r, v in tpc["setup_lines"].items()}
query_lines, opt_all, rest_all = {}, {}, {}
for q in tpc["queries"]:
    for part, acc in (("opt_lines", opt_all), ("rest_lines", rest_all)):
        for r, v in q[part].items():
            query_lines.setdefault(r, set()).update(v)
            acc.setdefault(r, set()).update(v)
# code that runs both inside the optimizer and outside it (parser, generator, expressions, executor)
shared = {r: opt_all.get(r, set()) & rest_all.get(r, set()) for r in query_lines}
files = sorted(query_lines)
results, picked, attempts = [], set(), 0
while len(results) < n and attempts < n * 80:
    attempts += 1
    rel = rng.choice(files)
    text = (Path(pristine) / rel).read_text()
    want_shared = rng.random() < 0.5
    lines_ok = shared[rel] if want_shared else query_lines[rel]
    pool = [s for s in candidate_sites(rel, text)
            if s[0] in lines_ok and s[0] not in setup.get(rel, set()) and scope_of(text, s[0]) == "function"]
    if not pool:
        continue
    line, col, end_col, old, new, kind = rng.choice(pool)
    if (rel, line, col) in picked:
        continue
    picked.add((rel, line, col))
    lines = text.split("\n")
    lines[line - 1] = lines[line - 1][:col] + new + lines[line - 1][end_col:]
    new_text = "\n".join(lines)
    try:
        compile(new_text, rel, "exec")
    except SyntaxError:
        continue
    patch = make_patch(rel, text, new_text)
    with tempfile.TemporaryDirectory() as data:
        os.makedirs(f"{data}/commits")
        Path(f"{data}/commits/m.patch").write_text(patch)
        Path(f"{data}/tests.json").write_text(Path(tests_path).read_text())
        env = dict(os.environ, BR_DATA=data, BR_SRC=pristine, BR_MAP=map_path, BR_TPCDS=tpcds_path)
        proc = subprocess.run([VENV_PY, "-S", selector], capture_output=True, text=True, env=env)
    line_doc = json.loads(proc.stdout.strip().split("\n")[0])
    selected = {tests[num]["id"] for w in line_doc["workers"] for num in w}
    note = [l for l in proc.stderr.splitlines() if "TPC-DS" in l]
    target = Path(work) / rel
    original = target.read_text()
    target.write_text(new_text)
    try:
        junit = f"/tmp/tpcds_junit_{seed}.xml"
        env = dict(os.environ, PYTHONPATH=work, SKIP_INTEGRATION="0")
        subprocess.run([VENV_PY, "-m", "pytest", TPCDS_ID, "-q", "-p", "no:cacheprovider", "--timeout", "600",
                        f"--junitxml={junit}"], cwd=work, env=env, capture_output=True, timeout=1500)
        failed = TPCDS_ID in failing_tests(junit)
    finally:
        target.write_text(original)
    rec = {"file": rel, "line": line, "kind": kind, "old": old, "new": new, "kept": TPCDS_ID in selected,
           "test_failed": failed, "unsound": failed and TPCDS_ID not in selected, "note": note[:1]}
    results.append(rec)
    print(json.dumps(rec), flush=True)
    json.dump(results, open(out_path, "w"), indent=1)
print(f"done: {len(results)} mutations, {sum(r['unsound'] for r in results)} unsound drops, "
      f"{sum(not r['kept'] for r in results)} drops", flush=True)
