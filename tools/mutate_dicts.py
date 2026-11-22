"""Make dict-entry mutants of the pinned sqlglot, for checking the dict-key refinement.

usage: python mutate_dicts.py <sqlglot repo> <operands.pkl.gz> <out dir> <count> [seed]

Picks entries of module/class-level dict literals whose value is a one-line literal
and changes one int, bool or str constant inside the value. Development only.
"""
import ast
import difflib
import gzip
import os
import pickle
import random
import sys

repo, ops_path, out_dir, count = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
rng = random.Random(int(sys.argv[5]) if len(sys.argv) > 5 else 1)
with gzip.open(ops_path, "rb") as fh:
    dicts = pickle.load(fh)["dicts"]

os.makedirs(os.path.join(out_dir, "commits"), exist_ok=True)
by_file = {}
for rel, line, col in dicts:
    by_file.setdefault(rel, set()).add((line, col))
cands = []
for rel, spots in sorted(by_file.items()):
    text = open(os.path.join(repo, rel), encoding="utf-8").read()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Dict) and (node.lineno, node.col_offset) in spots:
            for k, v in zip(node.keys, node.values):
                if k is None or v.lineno != v.end_lineno:
                    continue
                for c in ast.walk(v):
                    if isinstance(c, ast.Constant) and type(c.value) in (int, bool, str) and c.lineno == c.end_lineno:
                        cands.append((rel, c))
rng.shuffle(cands)
made, used = 0, set()
for rel, c in cands:
    if made >= count:
        break
    if (rel, c.lineno) in used:
        continue
    path = os.path.join(repo, rel)
    lines = open(path, encoding="utf-8").read().split("\n")
    b = lines[c.lineno - 1].encode()
    v = c.value
    if isinstance(v, bool):
        nv = repr(not v)
    elif isinstance(v, int):
        nv = repr(v + rng.choice([-1, 1]))
    else:
        nv = repr(v + "z")
    new = (b[:c.col_offset] + nv.encode() + b[c.end_col_offset:]).decode()
    new_lines = lines[:c.lineno - 1] + [new] + lines[c.lineno:]
    try:
        ast.parse("\n".join(new_lines))
    except SyntaxError:
        continue
    used.add((rel, c.lineno))
    diff = difflib.unified_diff([l + "\n" for l in lines], [l + "\n" for l in new_lines],
                                f"a/{rel}", f"b/{rel}", n=3)
    with open(os.path.join(out_dir, "commits", f"d{made:03d}.patch"), "w") as fh:
        fh.write(f"diff --git a/{rel} b/{rel}\n" + "".join(diff))
    made += 1
print(f"{made} mutants from {len(cands)} candidates", file=sys.stderr)
