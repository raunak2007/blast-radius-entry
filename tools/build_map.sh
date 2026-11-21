#!/usr/bin/env bash
# Rebuild blast_map.pkl.gz and sqlglot_src.tar.gz from the pinned sqlglot commit.
# Everything comes from running sqlglot's own tests at that commit; no patch or
# answer file is read. Run from the repo root, with the kit's sample/tests.json present.
set -euo pipefail
SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
WORK=${WORK:-/tmp/br-map}
mkdir -p "$WORK"
[ -d "$WORK/sqlglot" ] || git clone -q https://github.com/tobymao/sqlglot.git "$WORK/sqlglot"
git -C "$WORK/sqlglot" checkout -q "$SHA"

# the environment the organizers labeled in (kit's requirements.lock), plus coverage tools
uv venv -q --python 3.13 "$WORK/venv"
uv pip install -q --python "$WORK/venv/bin/python" -r requirements.lock coverage pytest-cov

# one run: per-test line coverage, files each test opens, dialect/expression usage.
# usage_plugin runs process-pool tasks inline (pickling them) so their work counts.
(cd "$WORK/sqlglot" && \
  PYTHONPATH="$WORK/sqlglot:$PWD/tools" SKIP_INTEGRATION=0 \
  USAGE_OUT="$WORK/usage.json" TRACE_ROOT="$WORK/sqlglot" TRACE_OUT="$WORK/trace.json" \
  COVERAGE_FILE="$WORK/coverage.db" \
  "$WORK/venv/bin/python" -m pytest tests -q -p no:cacheprovider -p usage_plugin -p trace_plugin \
    -n 2 --cov=sqlglot --cov-context=test --cov-report=)

PYTHONPATH="$WORK/sqlglot" "$WORK/venv/bin/python" tools/build_map.py \
  "$WORK/sqlglot" "$WORK/coverage.db" "$WORK/trace.json" "$WORK/usage.json" sample/tests.json blast_map.pkl.gz

# second run: the operands each comparison sees and the keys each dict literal gives out,
# per test (operand_plugin rewrites sqlglot's comparisons and module/class-level dicts)
rm -f "$WORK"/operands.pkl*
(cd "$WORK/sqlglot" && \
  PATH="$WORK/venv/bin:$PATH" PYTHONPATH="$WORK/sqlglot:$PWD/tools" SKIP_INTEGRATION=0 \
  USAGE_OUT="$WORK/usage-operands.json" OPERAND_ROOT="$WORK/sqlglot" OPERAND_OUT="$WORK/operands.pkl" \
  "$WORK/venv/bin/python" -m pytest tests -q -p no:cacheprovider -p usage_plugin -p operand_plugin -n 2)
"$WORK/venv/bin/python" tools/build_operands.py "$WORK/operands.pkl" blast_map.pkl.gz "$WORK/operands_whole.pkl.gz"
# repack for lazy loading: per-file comparison sites and dict reads, per-name dict keys as test masks
"$WORK/venv/bin/python" tools/build_lazy.py blast_map.pkl.gz dictkeys.pkl.gz "$WORK/dictkeys3.pkl.gz" \
  "$WORK/operands_whole.pkl.gz" operands.pkl.gz dictreads.pkl.gz && mv "$WORK/dictkeys3.pkl.gz" dictkeys.pkl.gz

(cd "$WORK/sqlglot" && find sqlglot tests -name '*.py' -not -path '*/__pycache__/*' | sort | \
  tar --owner=0 --group=0 --numeric-owner --mtime='2026-11-18 00:00:00' -czf "$OLDPWD/sqlglot_src.tar.gz" -T -)
