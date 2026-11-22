"""Repack tpcds.pkl.gz so each table's rows are a pickle inside the pickle.

usage: python pack_tpcds.py <tpcds.pkl.gz in> <tpcds.pkl.gz out>

The rows of the TPC-DS tables are most of the file, and only a replay that runs a query to
the end reads them, for the tables that query uses. Kept as bytes per table, they cost nothing
to load until then. Same information.
Development only.
"""
import gzip
import pickle
import sys

src, dst = sys.argv[1:3]
with gzip.open(src, "rb") as fh:
    data = pickle.load(fh)
if "tables" in data:
    data["table_blobs"] = {name: pickle.dumps(rows, protocol=4) for name, rows in data.pop("tables").items()}
with gzip.open(dst, "wb", compresslevel=9) as fh:
    pickle.dump(data, fh, protocol=4)
print("keys:", sorted(data), "row pickles:", sum(len(v) for v in data["table_blobs"].values()) // 1024, "KB in",
      len(data["table_blobs"]), "tables; largest:",
      sorted(((len(v) // 1024, k) for k, v in data["table_blobs"].items()), reverse=True)[:5])
