"""Edits of the kind the infection probes judge (dev tool, validation only): `is` <->
`==` and `is not` <-> `!=` swaps, plus small constant changes, inside function bodies
of modules that the expensive tests run (executor, planner, optimizer, generator,
expressions). Writes <out>/commits and <out>/meta.json; labels from
tools/label_pytest.py.

Usage: python tools/gen_probe_edits.py <sqlglot checkout> <out dir> <count> [seed]
"""

import ast
import hashlib
import json
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from gen_mutants import git_diff  # noqa: E402

FILES = ["sqlglot/executor/python.py", "sqlglot/executor/context.py", "sqlglot/executor/table.py",
         "sqlglot/executor/env.py", "sqlglot/planner.py", "sqlglot/optimizer/simplify.py",
         "sqlglot/optimizer/qualify_columns.py", "sqlglot/optimizer/scope.py",
         "sqlglot/optimizer/annotate_types.py", "sqlglot/optimizer/merge_subqueries.py",
         "sqlglot/optimizer/pushdown_predicates.py", "sqlglot/optimizer/unnest_subqueries.py",
         "sqlglot/optimizer/eliminate_subqueries.py", "sqlglot/optimizer/normalize.py",
         "sqlglot/generator.py", "sqlglot/expressions/core.py", "sqlglot/helper.py"]
SWAPS = [(r" is not ", " != "), (r" is ", " == "), (r" == ", " is "), (r" != ", " is not ")]


def function_lines(src):
    lines = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body_start = node.body[0].lineno
            if isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant):
                body_start = node.body[1].lineno if len(node.body) > 1 else node.end_lineno + 1
            lines.update(range(body_start, node.end_lineno + 1))
    return lines


def main():
    repo, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 5)
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    meta = {}
    pool = []
    for rel in FILES:
        src = open(os.path.join(repo, rel), encoding="utf-8").read()
        inside = function_lines(src)
        for i, line in enumerate(src.split("\n"), start=1):
            if i not in inside or line.lstrip().startswith(("#", "\"", "'")):
                continue
            for old, new in SWAPS:
                if re.search(old, line):
                    pool.append((rel, i, old, new))
    rng.shuffle(pool)
    for rel, i, old, new in pool:
        if len(meta) >= count:
            break
        src = open(os.path.join(repo, rel), encoding="utf-8").read()
        lines = src.split("\n")
        changed = re.sub(old, new, lines[i - 1], count=1)
        if changed == lines[i - 1]:
            continue
        lines[i - 1] = changed
        new_src = "\n".join(lines)
        try:
            ast.parse(new_src)
        except SyntaxError:
            continue
        cid = "p" + hashlib.sha1(f"{rel}:{i}:{old}".encode()).hexdigest()[:3]
        if cid in meta:
            continue
        with open(os.path.join(out, "commits", cid + ".patch"), "w") as handle:
            handle.write(git_diff(rel, src, new_src))
        meta[cid] = {"kind": "probe", "desc": [f"{rel}:{i} {old.strip()} -> {new.strip()}"]}
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
    print(f"{len(meta)} edits", file=sys.stderr)


if __name__ == "__main__":
    main()
