"""Generate patches that may leak state from one test to later ones (dev tool).

Edits inside function bodies that turn a copy into an alias or a rebinding into an
in-place change, preferring objects that outlive one test (module-level or
class-level names, attributes of self/cls): `.copy()` / `deepcopy(...)` / `copy.copy`
removed, `copy=True` -> `copy=False`, `dict(X)` / `list(X)` / `set(X)` / `X[:]` -> `X`,
`x = x + [...]` -> `x += [...]`. Label them with tools/label_pytest.py --all (the
whole suite in one pytest process), so a test that never runs the changed line but
reads a damaged shared object is seen failing.

Usage: python tools/gen_leaks.py <sqlglot checkout> <out dir> <count> [seed]
"""

from __future__ import annotations

import ast
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen_mutants import git_diff  # noqa: E402

DIRS = ("sqlglot/optimizer", "sqlglot/generator.py", "sqlglot/parser.py", "sqlglot/dialects/dialect.py",
        "sqlglot/expressions", "sqlglot/transforms.py", "sqlglot/helper.py", "sqlglot/generators",
        "sqlglot/parsers", "sqlglot/planner.py", "sqlglot/executor", "sqlglot/schema.py", "sqlglot/tokens.py",
        "sqlglot/tokenizer_core.py", "sqlglot/dialects")


def nonlocal_target(node, func) -> bool:
    """A Name not assigned in `func` (a global) or an attribute of self/cls/a module."""
    if isinstance(node, ast.Name):
        assigned = {n.id for n in ast.walk(func) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        params = {a.arg for a in func.args.args + func.args.kwonlyargs + func.args.posonlyargs}
        return node.id not in assigned and node.id not in params
    if isinstance(node, ast.Attribute):
        base = node.value
        return isinstance(base, ast.Name) and (base.id in ("self", "cls") or base.id[:1].isupper() or base.id in ("exp",))
    return False


def sites(path, text):
    tree = ast.parse(text)
    out = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if not hasattr(node, "lineno") or node.lineno != getattr(node, "end_lineno", None):
                continue
            # X.copy() -> X
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "copy" \
                    and not node.args and not node.keywords:
                inner = node.func.value
                out.append(("copy_removed", node, inner, nonlocal_target(inner, func)))
            # deepcopy(X) / copy.copy(X) / copy.deepcopy(X) -> X
            if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
                name = ast.unparse(node.func)
                if name in ("deepcopy", "copy.deepcopy", "copy.copy"):
                    out.append(("deepcopy_removed", node, node.args[0], nonlocal_target(node.args[0], func)))
                if name in ("dict", "list", "set", "tuple") and nonlocal_target(node.args[0], func):
                    out.append(("container_copy_removed", node, node.args[0], True))
            # copy=True -> copy=False
            if isinstance(node, ast.keyword) and node.arg == "copy" and isinstance(node.value, ast.Constant) \
                    and node.value.value is True:
                out.append(("copy_flag", node.value, None, False))
            # X[:] -> X
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice) and node.slice.lower is None \
                    and node.slice.upper is None and node.slice.step is None and isinstance(node.ctx, ast.Load):
                out.append(("slice_copy_removed", node, node.value, nonlocal_target(node.value, func)))
            # x = x + [...] -> x += [...]
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                    and isinstance(node.value, ast.BinOp) and isinstance(node.value.op, ast.Add) \
                    and isinstance(node.value.left, ast.Name) and node.value.left.id == node.targets[0].id \
                    and isinstance(node.value.right, (ast.List, ast.ListComp, ast.Tuple)):
                out.append(("augmented", node, None, False))
    return out


def edit(text, kind, node, inner):
    lines = text.splitlines(keepends=True)
    line = lines[node.lineno - 1].encode("utf-8")
    a, b = node.col_offset, node.end_col_offset
    if kind == "copy_flag":
        new = line[:a] + b"False" + line[b:]
    elif kind == "augmented":
        target = node.targets[0].id
        new = line[:a] + f"{target} += {ast.unparse(node.value.right)}".encode() + line[b:]
    else:
        new = line[:a] + ast.unparse(inner).encode() + line[b:]
    lines[node.lineno - 1] = new.decode("utf-8")
    return "".join(lines)


def main():
    repo, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 5
    rng = random.Random(seed)
    files = sorted(os.path.relpath(os.path.join(d, f), repo) for d, dn, fs in os.walk(os.path.join(repo, "sqlglot"))
                   for f in fs if f.endswith(".py") and "__pycache__" not in d)
    files = [f for f in files if f.startswith(DIRS)]
    pool = []
    for rel in files:
        text = open(os.path.join(repo, rel), encoding="utf-8").read()
        for kind, node, inner, shared in sites(rel, text):
            pool.append((rel, kind, node, inner, shared))
    # shared objects first (what can leak), then the rest
    shared = [p for p in pool if p[4]]
    other = [p for p in pool if not p[4]]
    rng.shuffle(shared)
    rng.shuffle(other)
    chosen = shared[: int(count * 0.7)] + other[: count - int(count * 0.7)]
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    meta = {}
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    for rel, kind, node, inner, is_shared in chosen:
        text = open(os.path.join(repo, rel), encoding="utf-8").read()
        new = edit(text, kind, node, inner)
        if new == text:
            continue
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        cid = "l" + "".join(rng.choice(alphabet) for _ in range(3))
        while cid in meta:
            cid = "l" + "".join(rng.choice(alphabet) for _ in range(3))
        with open(os.path.join(out, "commits", cid + ".patch"), "w") as handle:
            handle.write(git_diff(rel, text, new))
        old_line = text.splitlines()[node.lineno - 1].strip()
        new_line = new.splitlines()[node.lineno - 1].strip()
        meta[cid] = {"kind": kind, "shared": is_shared, "desc": [f"{rel}:{node.lineno} {old_line!r} -> {new_line!r}"]}
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
    print(f"wrote {len(meta)} patches ({sum(m['shared'] for m in meta.values())} on shared objects); "
          f"pool {len(pool)} sites, {len(shared)} shared", file=sys.stderr)


if __name__ == "__main__":
    main()
