"""Adversarial edits of class attributes that other, unrelated classes read by class name
(dev tool, validation only): Presto copies MySQL.TIME_MAPPING / TIME_FORMAT, Athena's
tokenizer merges Hive's and Trino's settings, PrestoGenerator reads Hive's inverse time
mapping, DateTrunc reads TimeUnit tables, merge_subqueries reads CopyBudget. Probes the
class-usage intersection of the class-attribute rule. Writes <out>/commits and
<out>/meta.json; labels from tools/label_pytest.py.

Usage: python tools/gen_xclass.py <sqlglot checkout> <out dir>
"""

import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from gen_mutants import git_diff  # noqa: E402

EDITS = [
    ("sqlglot/dialects/mysql.py", '        "%i": "%M",\n', '        "%i": "%m",\n'),
    ("sqlglot/dialects/mysql.py", "    TIME_FORMAT = \"'%Y-%m-%d %T'\"", "    TIME_FORMAT = \"'%Y-%m-%d %H'\""),
    ("sqlglot/dialects/hive.py", '        IDENTIFIERS = ["`"]', '        IDENTIFIERS = ["`", "@"]'),
    ("sqlglot/dialects/hive.py", '        "MMM": "%b",\n', '        "MMM": "%B",\n'),
    ("sqlglot/expressions/core.py", '        "H": "HOUR",\n', '        "H": "HOURS",\n'),
    ("sqlglot/optimizer/merge_subqueries.py", "    MAX_COPY_FACTOR = 8\n", "    MAX_COPY_FACTOR = 0\n"),
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
        cid = "x" + hashlib.sha1(f"{rel}:{old}".encode()).hexdigest()[:3]
        with open(os.path.join(out, "commits", cid + ".patch"), "w") as handle:
            handle.write(git_diff(rel, src, new_src))
        meta[cid] = {"kind": "xclass", "desc": [f"{rel}: {old.strip()[:60]!r} -> {new.strip()[:60]!r}"]}
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
    print(f"{len(meta)} edits", file=sys.stderr)


if __name__ == "__main__":
    main()
