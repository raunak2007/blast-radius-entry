"""Make comparison mutants of the pinned sqlglot, for checking the operand refinement.

usage: python mutate_compares.py <sqlglot repo> <operands.pkl.gz> <out dir> <count> [seed]

Picks comparison sites the test suite evaluates and writes one patch per mutant into
<out dir>/commits: an operator swap, or an int / str literal change. Development only;
nothing here is used by the selector.
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
    sites = pickle.load(fh)["sites"]

SWAP = {"==": ["!=", "is"], "!=": ["==", "is not"], "is": ["==", "is not"], "is not": ["!=", "is"],
        "<": ["<=", ">"], "<=": ["<", ">="], ">": [">=", "<"], ">=": [">", "<="],
        "in": ["not in"], "not in": ["in"]}
SYM = {"==": ast.Eq, "!=": ast.NotEq, "is": ast.Is, "is not": ast.IsNot, "<": ast.Lt, "<=": ast.LtE,
       ">": ast.Gt, ">=": ast.GtE, "in": ast.In, "not in": ast.NotIn}

os.makedirs(os.path.join(out_dir, "commits"), exist_ok=True)
keys = sorted(k for k, s in sites.items() if s["samples"] and not s["import"])
rng.shuffle(keys)
made = 0
for rel, line, col in keys:
    if made >= count:
        break
    path = os.path.join(repo, rel)
    text = open(path, encoding="utf-8").read()
    lines = text.split("\n")
    src = lines[line - 1]
    tree = ast.parse(text)
    node = next((n for n in ast.walk(tree) if isinstance(n, ast.Compare) and n.lineno == line
                 and n.col_offset == col), None)
    if node is None or node.end_lineno != line or len(node.ops) != 1:
        continue
    seg = src.encode()[col:node.end_col_offset].decode()
    right = node.comparators[0]
    choice = rng.random()
    new_seg = None
    if choice < 0.35 and isinstance(right, ast.Constant) and type(right.value) in (int, str) \
            and right.end_lineno == line and not isinstance(right.value, bool):
        lit = src.encode()[right.col_offset:right.end_col_offset].decode()
        v = right.value
        nv = repr(v + rng.choice([-1, 1])) if isinstance(v, int) else repr(v + "x" if rng.random() < .5 else v[:-1])
        rel_lo = right.col_offset - col
        b = seg.encode()
        new_seg = (b[:rel_lo] + nv.encode() + b[rel_lo + len(lit.encode()):]).decode()
    else:
        op = sites[(rel, line, col)]["op"]
        new_op = rng.choice(SWAP[op])
        left_end = node.left.end_col_offset - col
        right_start = right.col_offset - col
        b = seg.encode()
        middle = b[left_end:right_start].decode()
        if middle.strip() != op:
            continue
        new_seg = (b[:left_end] + f" {new_op} ".encode() + b[right_start:]).decode()
    if new_seg is None or new_seg == seg:
        continue
    bs = src.encode()
    new_line = (bs[:col] + new_seg.encode() + bs[node.end_col_offset:]).decode()
    new_lines = lines[:line - 1] + [new_line] + lines[line:]
    try:
        ast.parse("\n".join(new_lines))
    except SyntaxError:
        continue
    diff = difflib.unified_diff([l + "\n" for l in lines], [l + "\n" for l in new_lines],
                                f"a/{rel}", f"b/{rel}", n=3)
    name = f"m{made:03d}"
    with open(os.path.join(out_dir, "commits", name + ".patch"), "w") as fh:
        fh.write(f"diff --git a/{rel} b/{rel}\n" + "".join(diff))
    made += 1
print(f"{made} mutants", file=sys.stderr)
