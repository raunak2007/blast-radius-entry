"""Dev: how often expensive candidates fail when no cheap candidate fails."""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from selector.maps import Maps
m = Maps()
set_dir = sys.argv[1]
thr = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
tests = json.load(open(os.path.join(set_dir, 'tests.json')))['tests']
id_of = {t['number']: t['id'] for t in tests}
detail = json.load(open(os.path.join(set_dir, 'gt_detail.json')))
meta = json.load(open(os.path.join(set_dir, 'meta.json')))
rows = []
for cid, d in detail.items():
    if 'failing' not in d:
        continue
    cand = set(d['candidates'])
    exp = {n for n in cand if (m.vcpu[m.index[id_of[n]]] or 0) > thr}
    if not exp:
        continue
    cheap = cand - exp
    fail = set(d['failing'])
    rows.append((cid, meta[cid]['kind'], len(cheap), len(fail & cheap), sorted(id_of[n].split('::')[-1] for n in exp & fail), sorted(id_of[n].split('::')[-1] for n in exp)))
for r in sorted(rows, key=lambda r: (r[3] > 0, r[2])):
    print(r)
clean = [r for r in rows if r[3] == 0]
print('with expensive:', len(rows), 'cheap-clean:', len(clean), 'cheap-clean but expensive failed:', sum(1 for r in clean if r[4]))
