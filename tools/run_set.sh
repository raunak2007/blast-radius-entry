#!/bin/bash
# Run the selector natively on one data set and score it (dev tool).
# Usage: tools/run_set.sh <name> <data dir> <answers.json> [tag]
# Writes .work/out/<name><tag>.jsonl, .err, .score
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
NAME=$1; DATA=$2; ANSWERS=$3; TAG=${4:-}
OUT="$HERE/.work/out"; mkdir -p "$OUT"
TMPD="$HERE/.work/tmp/$NAME$TAG"; rm -rf "$TMPD"; mkdir -p "$TMPD"
export PATH="$HERE/.work/venv/bin:$PATH" BR_DATA="$DATA" BR_ROOT="$HERE/.work/sqlglot" BR_TMP="$TMPD" SKIP_INTEGRATION=0
export BR_TIME_LIMIT=${BR_TIME_LIMIT:-3000} BR_PLAN_DUMP="$OUT/$NAME$TAG.plans"
/usr/bin/time -p "$HERE/.work/venv/bin/python" "$HERE/selector/main.py" > "$OUT/$NAME$TAG.jsonl" 2> "$OUT/$NAME$TAG.err"
python3 "$HERE/../_kits/blast-radius/sample/score.py" "$DATA" "$ANSWERS" "$OUT/$NAME$TAG.jsonl" > "$OUT/$NAME$TAG.score" 2>&1
tail -3 "$OUT/$NAME$TAG.err" | tr '\n' ' '; echo
head -2 "$OUT/$NAME$TAG.score"
