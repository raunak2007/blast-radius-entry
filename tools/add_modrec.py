"""Add module attribute reads (tools/trace_modattrs.py) to the coverage map (build-time
tool).

Adds modrec: {"reads": {module\\tattr: set index}, "traced": set index}.

Usage: python tools/add_modrec.py <coverage.json.gz in> <mods.json> <out>
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

    table: dict = {}
    traced = 0
    for node, reads in rec["reads"].items():
        i = index.get(node)
        if i is None:
            continue
        bit = 1 << i
        traced |= bit
        for key in reads:
            table[key] = table.get(key, 0) | bit
    data["modrec"] = {"reads": {k: intern(m) for k, m in sorted(table.items())}, "traced": intern(traced)}
    data["sets"] = [format(m, "x") for m in sets]
    with gzip.open(out, "wt", compresslevel=9) as handle:
        json.dump(data, handle, separators=(",", ":"))
    print(f"modrec: {len(table)} module attributes, traced {bin(traced).count('1')}; size {os.path.getsize(out)}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
