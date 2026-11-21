"""Run, on each mutant, the tests the operand refinement dropped; any failure is a miss.

usage: python check_dropped.py <sqlglot git checkout> <mutant dir> <without.jsonl> <with.jsonl> <python>

Both jsonl files are selector outputs on the same mutants (BR_RUN=0), without and with
the operand profile. The checkout is restored after each mutant. Development only.
"""
import json
import os
import re
import shutil
import subprocess
import sys

repo, mdir, without_path, with_path, python = sys.argv[1:6]
tests = {t["number"]: t["id"] for t in json.load(open(os.path.join(mdir, "tests.json")))["tests"]}


def load(p):
    return {d["commit"]: {n for w in d["workers"] for n in w} for d in map(json.loads, open(p))}


before, after = load(without_path), load(with_path)
# a mutant reverted within the same second at the same size would reuse stale bytecode
for d, subdirs, _ in os.walk(repo):
    if "__pycache__" in subdirs:
        shutil.rmtree(os.path.join(d, "__pycache__"))
ENV = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=repo, PYTHONDONTWRITEBYTECODE="1")


def pytest(ids, parallel):
    extra = ["-n", "4"] if parallel else []
    return subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *extra, *ids],
                          cwd=repo, capture_output=True, text=True, env=ENV)

misses, total_dropped = [], 0
for commit in sorted(before):
    dropped = sorted(before[commit] - after[commit])
    if not dropped:
        continue
    total_dropped += len(dropped)
    patch = os.path.join(mdir, "commits", commit + ".patch")
    subprocess.run(["git", "-C", repo, "apply", patch], check=True)
    try:
        ids = [tests[n] for n in dropped]
        r = pytest(ids, True)
        ok = r.returncode == 0
        if not ok:
            # retry each failed test alone: memory or timing tests can fail in a mixed batch
            failed = sorted(set(re.findall(r"^(?:SUB)?FAILED(?:\[[^\]]*\]|\([^)]*\))? (\S+)", r.stdout, re.M)))
            again = [f for f in failed if pytest([f], False).returncode != 0]
            ok = bool(failed) and not again
            if ok:
                print(f"{commit}: {failed} failed only in the batch; passed alone", flush=True)
        print(f"{commit}: dropped {len(dropped)}, {'ok' if ok else 'MISS'}", flush=True)
        if not ok:
            misses.append(commit)
            print(r.stdout[-2500:])
    finally:
        subprocess.run(["git", "-C", repo, "checkout", "-q", "--", "."], check=True)
print(f"{len(misses)} mutants with misses: {misses}; {total_dropped} dropped tests checked")
