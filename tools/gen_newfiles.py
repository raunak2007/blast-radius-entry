"""Adversarial new files (dev tool, validation only): a package directory that shadows an
existing module (Python imports `x/__init__.py` before `x.py`), a module a lazy loader
would pick up by name, a new data file next to fixtures. Probes the new-file rule
(directory listers + lines mentioning the module's name). Writes <out>/commits and
<out>/meta.json; labels from tools/label_pytest.py.

Usage: python tools/gen_newfiles.py <out dir>
"""

import hashlib
import json
import os
import sys


def new_file_diff(rel, text):
    lines = text.splitlines(True)
    body = "".join("+" + line for line in lines)
    return (f"diff --git a/{rel} b/{rel}\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n+++ b/{rel}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n{body}")


FILES = [
    ("sqlglot/dialects/mysql/__init__.py", "raise ImportError('shadowed')\n"),
    ("sqlglot/optimizer/simplify/__init__.py", "SHADOW = True\n"),
    ("sqlglot/trie/__init__.py", "from enum import Enum\n\n\nclass TrieResult(Enum):\n    FAILED = 1\n"),
    ("sqlglot/dialects/tsql/__init__.py", "from sqlglot.dialects.dialect import Dialect\n\n\nclass TSQL(Dialect):\n    pass\n"),
    ("tests/fixtures/optimizer/zz_new.sql", "SELECT 1;\n"),
]


def main():
    out = sys.argv[1]
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    meta = {}
    for rel, text in FILES:
        cid = "n" + hashlib.sha1(rel.encode()).hexdigest()[:3]
        with open(os.path.join(out, "commits", cid + ".patch"), "w") as handle:
            handle.write(new_file_diff(rel, text))
        meta[cid] = {"kind": "newfile", "desc": [f"new {rel}"]}
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=1)
    print(f"{len(meta)} new files", file=sys.stderr)


if __name__ == "__main__":
    main()
