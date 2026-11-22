"""Adversarial edits of module-level values read through a module alias (dev tool,
validation only): `from sqlglot import expressions as exp` / `from sqlglot import exp`
then `exp.NAME`, where NAME is defined in a submodule of the expressions package.
Probes the name-reference narrowing of import-time changes. Writes <out>/commits and
<out>/meta.json; labels from tools/label_pytest.py.

Usage: python tools/gen_modrefs.py <sqlglot checkout> <out dir>
"""

import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from gen_mutants import git_diff  # noqa: E402

EDITS = [
    ("sqlglot/expressions/core.py", 'SAFE_IDENTIFIER_RE: t.Pattern[str] = re.compile(r"^[_a-zA-Z][\\w]*$")',
     'SAFE_IDENTIFIER_RE: t.Pattern[str] = re.compile(r"^[_a-zA-Z][\\w]+$")'),
    ("sqlglot/expressions/core.py", '    "where": False,\n', ""),
    ("sqlglot/expressions/builders.py", "CONSTANTS = (\n    Literal,\n    Boolean,\n    Null,\n)",
     "CONSTANTS = (\n    Literal,\n    Boolean,\n)"),
    ("sqlglot/expressions/aggregate.py", "PERCENTILES = (PercentileCont, PercentileDisc)", "PERCENTILES = (PercentileCont,)"),
    ("sqlglot/expressions/query.py", "UNWRAPPED_QUERIES = (Select, SetOperation)", "UNWRAPPED_QUERIES = (Select,)"),
    ("sqlglot/expressions/core.py", 'TABLE_PARTS = ("this", "db", "catalog")', 'TABLE_PARTS = ("this", "catalog", "db")'),
    ("sqlglot/expressions/core.py", 'COLUMN_PARTS = ("this", "table", "db", "catalog")', 'COLUMN_PARTS = ("this", "table", "db")'),
    ("sqlglot/expressions/builders.py", "OPERATORS = (AtTimeZone, Between, Binary, BitwiseNot, In, Neg, Not)",
     "OPERATORS = (AtTimeZone, Between, Binary, BitwiseNot, In, Neg)"),
    ("sqlglot/expressions/core.py", 'SQLGLOT_META: str = "sqlglot.meta"', 'SQLGLOT_META: str = "sqlglot.metax"'),
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
        cid = "m" + hashlib.sha1(f"{rel}:{old}".encode()).hexdigest()[:3]
        with open(os.path.join(out, "commits", cid + ".patch"), "w") as handle:
            handle.write(git_diff(rel, src, new_src))
        meta[cid] = {"kind": "modref", "desc": [f"{rel}: {old.strip()[:70]!r} -> {new.strip()[:70]!r}"]}
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
    print(f"{len(meta)} edits", file=sys.stderr)


if __name__ == "__main__":
    main()
