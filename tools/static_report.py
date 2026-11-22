"""Dev tool: show candidate sets for a directory of patches vs answers."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from selector.maps import Maps
from selector.patch import parse_patch
from selector.candidates import CandidateFinder

data_dir, answers_path, repo = sys.argv[1:4]
tests = json.load(open(os.path.join(data_dir, 'tests.json')))['tests']
answers = json.load(open(answers_path))['commits']
t0 = time.time(); maps = Maps(); print('maps load', round(time.time() - t0, 3))
finder = CandidateFinder(maps, repo)
sec = {t['id']: t['seconds'] for t in tests}
num2id = {t['number']: t['id'] for t in tests}
tot_unsound = 0
for cid in sorted(answers):
    patch = open(os.path.join(data_dir, 'commits', cid + '.patch')).read()
    t0 = time.time()
    cc = finder.analyze(parse_patch(patch))
    dt = time.time() - t0
    poss = set(maps.tests[i] for i in maps.tests_of(cc.possible))
    cert = set(maps.tests[i] for i in maps.tests_of(cc.certain))
    failing = set(num2id[n] for n in answers[cid]['failing'])
    missed = failing - poss
    tot_unsound += bool(missed)
    kinds = ','.join(f.kind + (':' + f.detail[:30] if f.detail else '') for f in cc.files)
    print(f"{cid} {answers[cid]['kind']:8} fail={len(failing):3} cand={len(poss):4} candsec={sum(sec[t] for t in poss):7.2f} cert={len(cert)} missed={len(missed)} {dt*1000:.0f}ms {kinds}")
    for f in cc.files:
        print('      ', f.path, 'func', bin(f.func).count('1'), 'imp', bin(f.imp).count('1'), 'data', bin(f.data).count('1'), 'other', bin(f.other).count('1'))
print('unsound commits', tot_unsound)
