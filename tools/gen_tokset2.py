"""Hand-picked edits of tokenizer settings beyond bool flips (dev tool, validation only):
set elements added/removed, dict entries removed/changed/renamed, strings and token
types changed. Writes <out>/commits/<id>.patch and <out>/meta.json; ground truth from
tools/label_pytest.py. Nothing here is used at decision time.

Usage: python tools/gen_tokset2.py <sqlglot checkout> <out dir>
"""

import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from gen_mutants import git_diff  # noqa: E402

EDITS = [
    ("sqlglot/dialects/duckdb.py", 'VAR_SINGLE_TOKENS = {"$"}', 'VAR_SINGLE_TOKENS = {"$", "@"}'),
    ("sqlglot/dialects/tsql.py", 'VAR_SINGLE_TOKENS = {"@", "$", "#"}', 'VAR_SINGLE_TOKENS = {"@", "$"}'),
    ("sqlglot/dialects/oracle.py", 'VAR_SINGLE_TOKENS = {"@", "$", "#"}', 'VAR_SINGLE_TOKENS = {"$", "#"}'),
    ("sqlglot/dialects/duckdb.py", "COMMANDS = tokens.Tokenizer.COMMANDS - {TokenType.SHOW}",
     "COMMANDS = tokens.Tokenizer.COMMANDS - {TokenType.SHOW, TokenType.EXECUTE}"),
    ("sqlglot/dialects/postgres.py", "COMMANDS = {*tokens.Tokenizer.COMMANDS, TokenType.LOCK}",
     "COMMANDS = {*tokens.Tokenizer.COMMANDS}"),
    ("sqlglot/dialects/sqlite.py", "COMMANDS = {*tokens.Tokenizer.COMMANDS, TokenType.REPLACE}",
     "COMMANDS = {*tokens.Tokenizer.COMMANDS}"),
    ("sqlglot/dialects/snowflake.py", "COMMANDS = tokens.Tokenizer.COMMANDS - {TokenType.SHOW}",
     "COMMANDS = tokens.Tokenizer.COMMANDS"),
    ("sqlglot/tokens.py", "COMMAND_PREFIX_TOKENS = {TokenType.SEMICOLON, TokenType.BEGIN}",
     "COMMAND_PREFIX_TOKENS = {TokenType.SEMICOLON}"),
    ("sqlglot/tokens.py", "TOKENS_PRECEDING_HINT = {TokenType.SELECT, TokenType.INSERT, TokenType.UPDATE, TokenType.DELETE}",
     "TOKENS_PRECEDING_HINT = {TokenType.SELECT, TokenType.INSERT, TokenType.UPDATE}"),
    ("sqlglot/tokens.py", "        TokenType.FETCH,\n        TokenType.SHOW,", "        TokenType.FETCH,"),
    ("sqlglot/dialects/hive.py", 'LONE_SURROGATE_REPLACEMENT = "?"', 'LONE_SURROGATE_REPLACEMENT = "!"'),
    ("sqlglot/dialects/duckdb.py", "HEREDOC_STRING_ALTERNATIVE = TokenType.PARAMETER", "HEREDOC_STRING_ALTERNATIVE = TokenType.VAR"),
    ("sqlglot/dialects/postgres.py", "HEREDOC_STRING_ALTERNATIVE = TokenType.PARAMETER", "HEREDOC_STRING_ALTERNATIVE = TokenType.STRING"),
    ("sqlglot/tokens.py", "HEREDOC_STRING_ALTERNATIVE = TokenType.VAR", "HEREDOC_STRING_ALTERNATIVE = TokenType.STRING"),
    ("sqlglot/dialects/hive.py", '            "S": "SMALLINT",\n', ""),
    ("sqlglot/dialects/hive.py", '            "D": "DOUBLE",', '            "D": "DECIMAL",'),
    ("sqlglot/dialects/hive.py", '            "Y": "TINYINT",', '            "Z": "TINYINT",'),
    ("sqlglot/dialects/postgres.py", '            "U": (16, 8, 8, 0x10FFFF),\n', ""),
    ("sqlglot/dialects/postgres.py", '            "x": (16, 1, 2, 0xFF),', '            "x": (16, 1, 3, 0xFF),'),
    ("sqlglot/dialects/snowflake.py", '"u": (16, 4, 4, 0xFFFF), "0": (8, 1, 3, 0xFF)}', '"0": (8, 1, 3, 0xFF)}'),
    ("sqlglot/dialects/clickhouse.py", 'NUMERIC_ESCAPES = {"x": (16, 2, 2, 0xFF)}', 'NUMERIC_ESCAPES = {"x": (16, 2, 2, 0xFF), "0": (8, 1, 3, 0o777)}'),
    ("sqlglot/dialects/redshift.py", 'NUMERIC_ESCAPES = {"0": (8, 1, 3, 0o777)}', "NUMERIC_ESCAPES = {}"),
    ("sqlglot/dialects/hive.py", 'LONE_SURROGATE_REPLACEMENT = "?"', 'LONE_SURROGATE_REPLACEMENT = ""'),
    ("sqlglot/tokens.py", 'HINT_START = "/*+"', 'HINT_START = "/*!"'),
]


def main():
    repo, out = sys.argv[1], sys.argv[2]
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    meta = {}
    for rel, old, new in EDITS:
        src = open(os.path.join(repo, rel), encoding="utf-8").read()
        if src.count(old) != 1:
            print(f"skip (found {src.count(old)}x): {rel}: {old!r}", file=sys.stderr)
            continue
        new_src = src.replace(old, new, 1)
        cid = "t" + hashlib.sha1(f"{rel}:{old}:{new}".encode()).hexdigest()[:3]
        with open(os.path.join(out, "commits", cid + ".patch"), "w") as handle:
            handle.write(git_diff(rel, src, new_src))
        meta[cid] = {"kind": "tokset2", "desc": [f"{rel}: {old.strip()!r} -> {new.strip()!r}"]}
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
    print(f"{len(meta)} edits", file=sys.stderr)


if __name__ == "__main__":
    main()
