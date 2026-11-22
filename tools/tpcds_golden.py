"""Build-time (runs in the runtime image): the pristine TPC-DS plan rows blast.plans compares against.

    python tools/tpcds_golden.py SRC_ROOT OUT_DIR

The same child the selector runs per commit, run once on the unpatched tree, so the selector
does not recompute it in every batch. A failed run writes nothing (the selector then computes it).
"""
import json
import os
import sys
from pathlib import Path

here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, here)
from blast import plans  # noqa: E402

root, out = sys.argv[1], sys.argv[2]
got = plans._run(Path(root), {}, None)
if got.get("ok") and got.get("rows"):
    os.makedirs(out, exist_ok=True)
    Path(out, "tpcds_golden.json").write_text(json.dumps(got["rows"]))
    print("tpcds golden rows", len(got["rows"]))
else:
    print("tpcds golden not built", got)
