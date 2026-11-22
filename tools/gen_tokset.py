"""Generate edits of tokenizer settings (dev tool, validation only).

For each Tokenizer class body (tokens.py and the dialects) and each setting that
Tokenizer._init_core passes to the core (selector/tokcore.py plumbing), make one
small edit of its value: flip a bool, change one entry of a dict (a number in a
tuple, a string), drop one entry, drop one element of a set/list, change a string.
Writes <out>/commits/<id>.patch and <out>/meta.json; ground truth comes from
tools/label_pytest.py. Nothing here is used at decision time.

Usage: python tools/gen_tokset.py <sqlglot checkout> <out dir> [max per setting]
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from gen_mutants import git_diff  # noqa: E402

from selector import tokcore  # noqa: E402


def edits_of(node: ast.expr, src_line: str, rng):
    """(old text, new text) candidates for one value, on its own line."""
    seg = src_line
    out = []
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        out.append((str(node.value), str(not node.value)))
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        out.append((repr(node.value), repr(node.value + "x")))
    elif isinstance(node, ast.Dict) and node.keys:
        i = rng.randrange(len(node.keys))
        v = node.values[i]
        if isinstance(v, ast.Tuple) and v.elts and isinstance(v.elts[1] if len(v.elts) > 1 else v.elts[0], ast.Constant):
            e = v.elts[1] if len(v.elts) > 1 else v.elts[0]
            if isinstance(e.value, int):
                text = ast.get_source_segment(SRC, v)
                inner = ast.get_source_segment(SRC, e)
                if text and inner:
                    new = text.replace(inner, str(e.value + 1), 1) if len(v.elts) == 1 else \
                        text[: text.index(",") + 1] + text[text.index(",") + 1:].replace(inner, str(e.value + 1), 1)
                    out.append((text, new))
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            text = ast.get_source_segment(SRC, v)
            out.append((text, repr(v.value + "X")))
    return [(a, b) for a, b in out if a in seg]


SRC = ""


def main():
    global SRC
    repo, out = sys.argv[1], sys.argv[2]
    per = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    rng = random.Random(7)
    settings = {s for v in tokcore.plumbing(open(os.path.join(repo, tokcore.TOKENS_FILE)).read()).values()
                for s, _, _ in v}
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    meta = {}
    files = [tokcore.TOKENS_FILE] + sorted(
        os.path.join("sqlglot/dialects", f) for f in os.listdir(os.path.join(repo, "sqlglot/dialects")) if f.endswith(".py"))
    for rel in files:
        SRC = open(os.path.join(repo, rel), encoding="utf-8").read()
        lines = SRC.split("\n")
        tree = ast.parse(SRC)
        for cls in ast.walk(tree):
            if not (isinstance(cls, ast.ClassDef) and (cls.name == "Tokenizer")):
                continue
            for stmt in cls.body:
                if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
                    continue
                name = stmt.targets[0].id
                if name not in settings:
                    continue
                count = 0
                for lineno in range(stmt.value.lineno, stmt.value.end_lineno + 1):
                    if count >= per:
                        break
                    line = lines[lineno - 1]
                    for old, new in edits_of(stmt.value, line, rng):
                        new_lines = list(lines)
                        new_lines[lineno - 1] = line.replace(old, new, 1)
                        new_src = "\n".join(new_lines)
                        if new_src == SRC:
                            continue
                        try:
                            ast.parse(new_src)
                        except SyntaxError:
                            continue
                        cid = hashlib.sha1(f"{rel}:{lineno}:{new}".encode()).hexdigest()[:4]
                        with open(os.path.join(out, "commits", cid + ".patch"), "w") as handle:
                            handle.write(git_diff(rel, SRC, new_src))
                        meta[cid] = {"kind": "tokset", "desc": [f"{rel}:{lineno} {name}: {old} -> {new}"]}
                        count += 1
                        break
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
    print(f"{len(meta)} edits", file=sys.stderr)


if __name__ == "__main__":
    main()
