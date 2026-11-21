"""Mutants inside multi-line function statements that run at import (a builder called while a
module is imported) where the mutated line itself never ran at import (a lambda or closure body
the builder returns), for checking the per-instruction import-time test. Development only.

usage: python mutate_nested.py <sqlglot repo> <blast_map.pkl.gz> <out dir> <count> [seed]
"""
import ast
import difflib
import gzip
import os
import pickle
import random
import sys

repo, map_path, out_dir, count = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
rng = random.Random(int(sys.argv[5]) if len(sys.argv) > 5 else 1)
with gzip.open(map_path, "rb") as fh:
    doc = pickle.load(fh)
os.makedirs(os.path.join(out_dir, "commits"), exist_ok=True)
SWAP = {ast.Eq: "!=", ast.NotEq: "==", ast.Lt: "<=", ast.LtE: "<", ast.Gt: ">=", ast.GtE: ">", ast.Is: "==",
        ast.IsNot: "!="}
SYM = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Is: "is",
       ast.IsNot: "is not"}
cands = []
for rel, imp in doc["import_lines"].items():
    if not rel.startswith("sqlglot/"):
        continue
    imp = set(imp)
    covered = doc["lines"].get(rel, {})
    path = os.path.join(repo, rel)
    text = open(path, encoding="utf-8").read()
    tree = ast.parse(text)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for st in ast.walk(fn):
            if not isinstance(st, ast.stmt) or st is fn or st.end_lineno == st.lineno:
                continue
            span = range(st.lineno, st.end_lineno + 1)
            if not any(ln in imp for ln in span):
                continue
            for node in ast.walk(st):
                ln = getattr(node, "lineno", None)
                if ln is None or ln in imp or ln not in covered or getattr(node, "end_lineno", ln) != ln:
                    continue
                if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in SWAP:
                    cands.append((rel, "cmp", node.lineno, node.col_offset, node.end_col_offset))
                elif isinstance(node, ast.Constant) and type(node.value) in (int, bool):
                    cands.append((rel, "const", node.lineno, node.col_offset, node.end_col_offset))
                elif isinstance(node, ast.BoolOp):
                    cands.append((rel, "boolop", node.lineno, node.col_offset, node.end_col_offset))
cands = sorted(set(cands))
rng.shuffle(cands)
made = 0
for rel, kind, line, col, end in cands:
    if made >= count:
        break
    path = os.path.join(repo, rel)
    text = open(path, encoding="utf-8").read()
    lines = text.split("\n")
    src = lines[line - 1].encode()
    seg = src[col:end].decode()
    tree = ast.parse(seg, mode="eval").body if seg.strip() else None
    new_seg = None
    if kind == "const":
        v = ast.literal_eval(seg)
        new_seg = repr(not v) if isinstance(v, bool) else repr(v + rng.choice([-1, 1]))
    elif kind == "cmp" and isinstance(tree, ast.Compare):
        op = type(tree.ops[0])
        a = src[col:col].decode()
        left_end = tree.left.end_col_offset
        right_start = tree.comparators[0].col_offset
        b = seg.encode()
        middle = b[left_end:right_start].decode()
        if middle.strip() != SYM[op]:
            continue
        new_seg = (b[:left_end] + f" {SWAP[op]} ".encode() + b[right_start:]).decode()
    elif kind == "boolop" and isinstance(tree, ast.BoolOp):
        word = " and " if isinstance(tree.op, ast.And) else " or "
        if seg.count(word) != 1:
            continue
        new_seg = seg.replace(word, " or " if word == " and " else " and ")
    if not new_seg or new_seg == seg:
        continue
    new_line = (src[:col] + new_seg.encode() + src[end:]).decode()
    new_lines = lines[:line - 1] + [new_line] + lines[line:]
    try:
        ast.parse("\n".join(new_lines))
    except SyntaxError:
        continue
    diff = difflib.unified_diff([l + "\n" for l in lines], [l + "\n" for l in new_lines], f"a/{rel}", f"b/{rel}", n=3)
    with open(os.path.join(out_dir, "commits", f"n{made:03d}.patch"), "w") as fh:
        fh.write(f"diff --git a/{rel} b/{rel}\n" + "".join(diff))
    made += 1
print(f"{made} mutants from {len(cands)} candidates", file=sys.stderr)
