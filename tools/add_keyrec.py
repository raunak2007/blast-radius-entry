"""Add string-keyed dict lookup records (tools/trace_keys.py) to the coverage map
(build-time tool).

Adds keyrec: {"lookups": {holder\\tattr: {key or "*": set index}}, "traced": set index,
"holders": [every recorded holder\\tattr], "unrecordable": [...]}.

Usage: python tools/add_keyrec.py <coverage.json.gz in> <keys.json> <out>
"""

import gzip
import json
import os
import sys


def main():
    src, rec_path, out = sys.argv[1:4]
    data = json.load(gzip.open(src, "rt"))
    rec = json.load(open(rec_path))
    meta = json.load(open(rec_path + ".meta"))
    index = {node: i for i, node in enumerate(data["tests"])}
    sets = [int(h, 16) for h in data["sets"]]
    set_index = {m: k for k, m in enumerate(sets)}

    def intern(mask):
        k = set_index.get(mask)
        if k is None:
            k = set_index[mask] = len(sets)
            sets.append(mask)
        return k

    table: dict = {}
    traced = 0
    for node, lookups in rec["lookups"].items():
        i = index.get(node)
        if i is None:
            continue
        bit = 1 << i
        traced |= bit
        for holder, keys in lookups.items():
            slot = table.setdefault(holder, {})
            for k in keys:
                slot[k] = slot.get(k, 0) | bit
    data["keyrec"] = {
        "lookups": {h: {k: intern(m) for k, m in sorted(slot.items())} for h, slot in sorted(table.items())},
        "traced": intern(traced),
        "holders": meta["holders"],
        "unrecordable": rec["unrecordable"],
    }
    data["sets"] = [format(m, "x") for m in sets]
    with gzip.open(out, "wt", compresslevel=9) as handle:
        json.dump(data, handle, separators=(",", ":"))
    keys = sum(len(v) for v in table.values())
    print(f"keyrec: {len(table)} dicts, {keys} keys, traced {bin(traced).count('1')}; size {os.path.getsize(out)}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
