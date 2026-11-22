"""Add typed coverage (tools/trace_typed.py) to maps/coverage.json.gz (build-time tool).

Adds
  typed:     {file: {line: {receiver class key: set index}}}  tests whose run of that
             line had `self`/`cls` of that class
  typed_mro: {receiver class key: [keys of its runtime MRO]}

Usage: python tools/add_typed.py <coverage.json.gz in> <typed.json> <out>
"""

import gzip
import json
import os
import sys


def main():
    src, typed_path, out = sys.argv[1:4]
    data = json.load(gzip.open(src, "rt"))
    typed = json.load(open(typed_path))
    index = {node: i for i, node in enumerate(data["tests"])}
    sets = [int(h, 16) for h in data["sets"]]
    set_index = {m: k for k, m in enumerate(sets)}

    def intern(mask):
        k = set_index.get(mask)
        if k is None:
            k = set_index[mask] = len(sets)
            sets.append(mask)
        return k

    def build(records):
        table = {}
        for node, files in records.items():
            i = index.get(node)
            if i is None:
                continue
            bit = 1 << i
            for f, lines in files.items():
                for line, keys in lines.items():
                    bucket = table.setdefault(f, {}).setdefault(line, {})
                    for key in keys:
                        bucket[key] = bucket.get(key, 0) | bit
        return table, {f: {line: {k: intern(m) for k, m in sorted(b.items())}
                           for line, b in sorted(lines.items(), key=lambda x: int(x[0]))}
                       for f, lines in sorted(table.items())}

    table, data["typed"] = build(typed["typed"])
    # receivers of self.dialect.NAME reads (class of the dialect object)
    _, data["typed_dialect"] = build(typed.get("typed_dialect", {}))
    data["typed_mro"] = typed["mro"]
    data["sets"] = [format(m, "x") for m in sets]
    with gzip.open(out, "wt", compresslevel=9) as handle:
        json.dump(data, handle, separators=(",", ":"))
    print(f"typed lines {sum(len(v) for v in table.values())} in {len(table)} files; "
          f"{len(typed['typed'])} tests; size {os.path.getsize(out)}", file=sys.stderr)


if __name__ == "__main__":
    main()
