"""Fold labels from tools/label_pytest.py into validation/<set> (dev tool).

Copies each newly labelled patch into validation/<set>/commits, adds its answer
to answers.json and its meta (with "labeller": "pytest") to meta.json. Answers
already present from the older in-process labeller are replaced by the pytest
label when one exists (and the difference is reported).

Usage: python tools/merge_labels.py <label dir> <validation set dir>
"""

import json
import os
import shutil
import sys


def main():
    src, dst = sys.argv[1:3]
    detail = json.load(open(os.path.join(src, "label_detail.json")))
    meta_src = json.load(open(os.path.join(src, "meta.json")))
    os.makedirs(os.path.join(dst, "commits"), exist_ok=True)
    ans_path = os.path.join(dst, "answers.json")
    meta_path = os.path.join(dst, "meta.json")
    answers = json.load(open(ans_path)) if os.path.exists(ans_path) else {"commits": {}}
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    added = changed = 0
    for cid, d in sorted(detail.items()):
        if "failing" not in d or cid not in meta_src:
            continue
        old = answers["commits"].get(cid)
        if old is not None and sorted(old["failing"]) != sorted(d["failing"]):
            print(f"{cid}: old label {old['failing'][:10]} -> pytest {d['failing'][:10]}", file=sys.stderr)
            changed += 1
        if old is None:
            added += 1
        shutil.copy(os.path.join(src, "commits", cid + ".patch"), os.path.join(dst, "commits", cid + ".patch"))
        answers["commits"][cid] = {"failing": d["failing"], "kind": meta_src[cid]["kind"]}
        meta[cid] = dict(meta_src[cid], labeller="pytest")
    json.dump(answers, open(ans_path, "w"), indent=0)
    json.dump(meta, open(meta_path, "w"), indent=1)
    print(f"{dst}: added {added}, relabelled-different {changed}, total {len(answers['commits'])}", file=sys.stderr)


if __name__ == "__main__":
    main()
