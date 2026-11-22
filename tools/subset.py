"""Dev: make a data dir with only the labeled commits of a validation set."""
import json, os, shutil, sys
src, dst = sys.argv[1:3]
detail = json.load(open(os.path.join(src, 'gt_detail.json')))
meta = json.load(open(os.path.join(src, 'meta.json')))
os.makedirs(os.path.join(dst, 'commits'), exist_ok=True)
for f in os.listdir(os.path.join(dst, 'commits')):
    os.remove(os.path.join(dst, 'commits', f))
answers = {'commits': {}}
for cid, d in detail.items():
    if 'failing' in d:
        shutil.copy(os.path.join(src, 'commits', cid + '.patch'), os.path.join(dst, 'commits'))
        answers['commits'][cid] = {'failing': d['failing'], 'kind': meta[cid]['kind']}
shutil.copy(os.path.join(src, 'tests.json'), dst)
json.dump(answers, open(os.path.join(dst, 'answers.json'), 'w'))
json.dump(meta, open(os.path.join(dst, 'meta.json'), 'w'))
print(len(answers['commits']), 'commits')
