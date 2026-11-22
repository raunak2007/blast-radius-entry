"""Add tokenizer-core owner records (tools/trace_tokcore.py) to the coverage map
(build-time tool).

Adds
  tokcore: {"lines": {line of sqlglot/tokenizer_core.py: {owner class key: set index}},
            "traced": set index of the tests that have a record}
and merges the owners' MROs into typed_mro.

Per-case records (<tokcore.json>.cases/, tools/trace_tokcore.py --cases) set the bits
of the per-case pseudo-tests; a warm case record is unioned with its test's cold
zero-case record (imports and the code outside the loops). A pseudo-test without a
record stays untraced (kept whenever it ran the _init_core line).

Usage: python tools/add_tokcore.py <coverage.json.gz in> <tokcore.json> <out>
"""

import gzip
import json
import os
import sys


def main():
    src, rec_path, out = sys.argv[1:4]
    data = json.load(gzip.open(src, "rt"))
    rec = json.load(open(rec_path))
    index = {node: i for i, node in enumerate(data["tests"])}
    sets = [int(h, 16) for h in data["sets"]]
    set_index = {m: k for k, m in enumerate(sets)}

    def intern(mask):
        k = set_index.get(mask)
        if k is None:
            k = set_index[mask] = len(sets)
            sets.append(mask)
        return k

    lines = {}
    traced = 0
    for node, by_line in rec["lines"].items():
        i = index.get(node)
        if i is None:
            continue
        bit = 1 << i
        traced |= bit
        for line, owners in by_line.items():
            slot = lines.setdefault(line, {})
            for owner in owners:
                slot[owner] = slot.get(owner, 0) | bit
    case_dir = rec_path + ".cases"
    if os.path.isdir(case_dir):
        records = [json.load(open(os.path.join(case_dir, n))) for n in sorted(os.listdir(case_dir)) if n.endswith(".json")]
        outside = {r["test"]: r for r in records if r["fixture"] is None and not r["failed"]}
        added = 0
        for r in records:
            i = index.get(r["pseudo"])
            if i is None or r["failed"]:
                continue
            by_line = {k: set(v) for k, v in r["lines"].items()}
            if r.get("warm"):
                cold = outside.get(r["test"])
                if cold is None:
                    continue  # no cold zero-case record: leave the case untraced
                for k, v in cold["lines"].items():
                    by_line.setdefault(k, set()).update(v)
            bit = 1 << i
            traced |= bit
            added += 1
            for line, owners in by_line.items():
                slot = lines.setdefault(line, {})
                for owner in owners:
                    slot[owner] = slot.get(owner, 0) | bit
            for key, bases in r["mro"].items():
                rec["mro"].setdefault(key, bases)
        print(f"case records {added} of {len(records)}", file=sys.stderr)
    data["tokcore"] = {
        "lines": {line: {o: intern(m) for o, m in sorted(slot.items())} for line, slot in sorted(lines.items(), key=lambda x: int(x[0]))},
        "traced": intern(traced),
    }
    mro = data.setdefault("typed_mro", {})
    for key, bases in rec["mro"].items():
        mro[key] = sorted(set(mro.get(key, [])) | set(bases))
    data["sets"] = [format(m, "x") for m in sets]
    with gzip.open(out, "wt", compresslevel=9) as handle:
        json.dump(data, handle, separators=(",", ":"))
    print(f"tokcore lines {len(lines)}; traced {bin(traced).count('1')} tests; size {os.path.getsize(out)}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
