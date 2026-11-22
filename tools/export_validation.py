"""Dev: copy labeled validation sets into validation/<name>/ (patches, meta, labels).

The labels are for the harness only; the image never sees validation/ (.dockerignore).
Usage: python tools/export_validation.py <set dir> <name>
"""
import json
import os
import shutil
import sys

src, name = sys.argv[1:3]
dst = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "validation", name)
shutil.rmtree(dst, ignore_errors=True)
os.makedirs(os.path.join(dst, "commits"))
detail = json.load(open(os.path.join(src, "gt_detail.json")))
meta = json.load(open(os.path.join(src, "meta.json")))
answers = {"commits": {}}
for cid, d in sorted(detail.items()):
    if "failing" not in d:
        continue
    shutil.copy(os.path.join(src, "commits", cid + ".patch"), os.path.join(dst, "commits"))
    answers["commits"][cid] = {"failing": d["failing"], "kind": meta[cid]["kind"]}
json.dump(answers, open(os.path.join(dst, "answers.json"), "w"), indent=0)
json.dump({c: meta[c] for c in answers["commits"]}, open(os.path.join(dst, "meta.json"), "w"), indent=1)
print(name, len(answers["commits"]), "labeled commits")
