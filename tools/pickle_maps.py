"""Convert maps/*.json[.gz] to maps/*.pickle at image build time (faster to load).

The coverage map is stored with integer line keys. Run: python tools/pickle_maps.py maps
"""

import gzip
import json
import pickle
import sys
from pathlib import Path


def main() -> None:
    maps = Path(sys.argv[1])
    for path in sorted(maps.glob("*.json*")):
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
        stem = path.name.removesuffix(".gz").removesuffix(".json")
        if stem == "coverage":
            data = {f: {int(k): v for k, v in lines.items()} for f, lines in data.items()}
        with open(maps / f"{stem}.pickle", "wb") as handle:
            pickle.dump(data, handle, protocol=pickle.HIGHEST_PROTOCOL)


if __name__ == "__main__":
    main()
