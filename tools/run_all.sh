#!/bin/bash
# Run every local set (kit sample + labelled validation sets) and print breakdowns.
# Usage: tools/run_all.sh <tag> [sets...]
HERE="$(cd "$(dirname "$0")/.." && pwd)"; TAG=$1; shift
SETS=${@:-sample val1 val2 val4 val3 val5 val6 val7 val8 val9 val10 val11 val12 val13 val14 val15 val16}
for s in $SETS; do
  if [ $s = sample ]; then
    A="$HERE/../_kits/blast-radius/sample/sample/answers.json"
  else
    A="$HERE/validation/$s/answers.json"
    [ -f "$A" ] || continue
    mkdir -p "$HERE/.work/data/$s"
    ln -sfn "$HERE/validation/$s/commits" "$HERE/.work/data/$s/commits"
    ln -sf "$HERE/validation/tests.json" "$HERE/.work/data/$s/tests.json"
  fi
  echo "== $s"; "$HERE/tools/run_set.sh" $s "$HERE/.work/data/$s" "$A" "$TAG"
  python3 "$HERE/tools/breakdown.py" "$HERE/.work/data/$s" "$A" "$HERE/.work/out/$s$TAG.jsonl" --plans "$HERE/.work/out/$s$TAG.plans" --rows 0 | head -1
done
