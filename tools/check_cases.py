"""Check selector/cases.py's fixture splitting against the real tests/helpers.py (dev tool).

Usage: python tools/check_cases.py <sqlglot checkout>
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
repo = os.path.realpath(sys.argv[1])
sys.path.insert(1, repo)

from selector import cases  # noqa: E402
from tests import helpers  # noqa: E402

bad = 0
checked = 0
fixtures = os.path.join(repo, "tests", "fixtures")
for dirpath, _, names in os.walk(fixtures):
    for name in names:
        if not name.endswith(".sql"):
            continue
        rel = os.path.relpath(os.path.join(dirpath, name), fixtures)
        text = open(os.path.join(dirpath, name), "rb").read().decode("utf-8")
        try:
            real_pairs = list(helpers.load_sql_fixture_pairs(rel))
            ok_pairs = True
        except Exception:  # noqa: BLE001 - files that are not pair files
            ok_pairs = False
        if ok_pairs:
            mine = cases.pair_cases(text)
            same = len(mine) == len(real_pairs) and all(
                helpers._extract_meta(s) == (sql, meta) and e == expected
                for (s, e), (meta, sql, expected) in zip(mine, real_pairs))
            if not same:
                bad += 1
                print("PAIRS MISMATCH", rel)
        real_lines = list(helpers.load_sql_fixtures(rel))
        if cases.line_cases(text) != real_lines:
            bad += 1
            print("LINES MISMATCH", rel)
        checked += 1
# a few synthetic edge cases: CRLF, comments, trailing text
for text in ["SELECT 1;\r\nSELECT 2;\r\n", "-- c\n# a: b\nSELECT 1;\nSELECT 1;\n--x\nSELECT 2;\nX", "x;y;z", ""]:
    path = os.path.join(fixtures, "__br_check__.sql")
    try:
        with open(path, "wb") as handle:
            handle.write(text.encode("utf-8"))
        real = [(sql, e) for meta, sql, e in helpers.load_sql_fixture_pairs("__br_check__.sql")]
        mine = [(helpers._extract_meta(s)[0], e) for s, e in cases.pair_cases(text)]
        if real != mine or list(helpers.load_sql_fixtures("__br_check__.sql")) != cases.line_cases(text):
            bad += 1
            print("SYNTHETIC MISMATCH", repr(text))
    finally:
        os.remove(path)
print(f"checked {checked} fixtures, {bad} mismatches")
sys.exit(1 if bad else 0)
