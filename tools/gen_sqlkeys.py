"""Edits of dicts keyed by SQL text (dev tool, validation only): rename one key of
tokens.py KEYWORDS (type names the library itself parses, e.g. DataType.build("INT"))
or of parser.py FUNCTIONS / FUNCTION_PARSERS. Probes the text rule for SQL-keyed dicts.
Writes <out>/commits/<id>.patch and <out>/meta.json; labels from tools/label_pytest.py.

Usage: python tools/gen_sqlkeys.py <sqlglot checkout> <out dir>
"""

import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from gen_mutants import git_diff  # noqa: E402

KEYWORDS = ["INT", "BIGINT", "DATE", "TIMESTAMP", "VARCHAR", "BOOLEAN", "DOUBLE", "TEXT", "DECIMAL", "ARRAY",
            "STRUCT", "MAP", "JSON", "INTERVAL"]
PARSER_KEYS = ['        "CONCAT": lambda args, dialect: exp.Concat(', '        "LOG": build_logarithm,',
               '        "CAST": lambda self: self._parse_cast(self.STRICT_CAST),',
               '        "EXTRACT": lambda self: self._parse_extract(),',
               '        "TRY_CAST": lambda self: self._parse_cast(False, safe=True),']


def main():
    repo, out = sys.argv[1], sys.argv[2]
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    meta = {}
    edits = [("sqlglot/tokens.py", f'        "{w}": TokenType.{w},', f'        "{w}X": TokenType.{w},') for w in KEYWORDS]
    edits += [("sqlglot/parser.py", line, line.replace('": ', 'X": ', 1)) for line in PARSER_KEYS]
    for rel, old, new in edits:
        src = open(os.path.join(repo, rel), encoding="utf-8").read()
        if src.count(old) != 1:
            print(f"skip ({src.count(old)}x): {old.strip()}", file=sys.stderr)
            continue
        new_src = src.replace(old, new, 1)
        cid = "k" + hashlib.sha1(f"{rel}:{old}".encode()).hexdigest()[:3]
        with open(os.path.join(out, "commits", cid + ".patch"), "w") as handle:
            handle.write(git_diff(rel, src, new_src))
        meta[cid] = {"kind": "sqlkey", "desc": [f"{rel}: {old.strip()!r} -> {new.strip()!r}"]}
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
    print(f"{len(meta)} edits", file=sys.stderr)


if __name__ == "__main__":
    main()
