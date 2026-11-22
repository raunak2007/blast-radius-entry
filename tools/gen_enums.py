"""Adversarial edits of enum members and other class attributes read through the class
object rather than an instance (dev tool, validation only): TokenType / DType members,
dialect settings read as `Dialect.X`. Probes the class-usage intersection of the
class-attribute rule. Writes <out>/commits and <out>/meta.json; labels from
tools/label_pytest.py.

Usage: python tools/gen_enums.py <sqlglot checkout> <out dir>
"""

import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from gen_mutants import git_diff  # noqa: E402

EDITS = [
    ("sqlglot/tokenizer_core.py", "    L_PAREN = auto()\n    R_PAREN = auto()\n", "    R_PAREN = auto()\n    L_PAREN = auto()\n"),
    ("sqlglot/tokenizer_core.py", "    R_PAREN = auto()\n", "    R_PAREN = 1\n"),
    ("sqlglot/expressions/datatypes.py", "    BIGINT = auto()\n", '    BIGINT = "BIG_INT"\n'),
]


def main():
    repo, out = sys.argv[1], sys.argv[2]
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    meta = {}
    for rel, old, new in EDITS:
        src = open(os.path.join(repo, rel), encoding="utf-8").read()
        if src.count(old) != 1:
            print(f"skip ({src.count(old)}x): {old.strip()[:60]}", file=sys.stderr)
            continue
        new_src = src.replace(old, new, 1)
        cid = "e" + hashlib.sha1(f"{rel}:{old}:{new}".encode()).hexdigest()[:3]
        with open(os.path.join(out, "commits", cid + ".patch"), "w") as handle:
            handle.write(git_diff(rel, src, new_src))
        meta[cid] = {"kind": "enum", "desc": [f"{rel}: {old.strip()[:60]!r} -> {new.strip()[:60]!r}"]}
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
    print(f"{len(meta)} edits", file=sys.stderr)


if __name__ == "__main__":
    main()
