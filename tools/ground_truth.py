"""Label a validation set by actually running tests on each patched tree (dev tool).

For each patch: run every candidate test (the selector's sound superset) plus a
random spot-check sample of other tests that import a changed module, in a
patched overlay, in-process. Failing spot-check tests reveal unsound candidate
sets. Writes answers.json (same format as the sample) and gt_detail.json.

Usage: python tools/ground_truth.py <set dir> <sqlglot checkout> [--spot N] [--jobs N] [--only id,id]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from selector import policy  # noqa: E402
from selector.candidates import CandidateFinder  # noqa: E402
from selector.maps import Maps  # noqa: E402
from selector.patch import parse_patch  # noqa: E402
from selector.verify import Job, run_jobs  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("set_dir")
    parser.add_argument("repo")
    parser.add_argument("--spot", type=int, default=40)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--only", default=None)
    parser.add_argument("--max-cpu", type=float, default=400.0, help="skip patches whose candidates cost more")
    parser.add_argument("--broad", action="store_true", help="run every cheap importer of a changed module, not a sample")
    options = parser.parse_args()

    repo = os.path.realpath(options.repo)
    tests = json.load(open(os.path.join(options.set_dir, "tests.json")))["tests"]
    id_of = {t["number"]: t["id"] for t in tests}
    number_of = {t["id"]: t["number"] for t in tests}
    seconds = {t["number"]: t["seconds"] for t in tests}
    meta = json.load(open(os.path.join(options.set_dir, "meta.json")))
    detail_path = os.path.join(options.set_dir, "gt_detail.json")
    detail = json.load(open(detail_path)) if os.path.exists(detail_path) else {}
    maps = Maps()
    finder = CandidateFinder(maps, repo)
    rng = random.Random(5)
    os.environ["SKIP_INTEGRATION"] = "0"

    cids = sorted(meta)
    if options.only:
        cids = [c for c in cids if c in options.only.split(",")]
    work = []
    for cid in cids:
        if cid in detail:
            continue
        text = open(os.path.join(options.set_dir, "commits", cid + ".patch")).read()
        changes = parse_patch(text)
        found = finder.analyze(changes)
        cand = {number_of[maps.tests[i]] for i in maps.tests_of(found.possible)}
        certain = {number_of[maps.tests[i]] for i in maps.tests_of(found.certain)}
        importers = 0
        files = {}
        for change, report in zip(changes, found.files):
            if change.old_path:
                importers |= maps.import_mask(change.old_path) | maps.open_mask(change.old_path)
            if change.is_deleted:
                files[change.old_path] = None
            else:
                if change.is_rename:
                    files[change.old_path] = None
                files[change.path] = (report.new_text or "").encode("utf-8")
        pool = [number_of[maps.tests[i]] for i in maps.tests_of(importers)]
        pool = [n for n in pool if n not in cand and (maps.vcpu[maps.index[id_of[n]]] or 1) < 0.5]
        if options.broad:
            spot = {n for n in pool if (maps.vcpu[maps.index[id_of[n]]] or 1) < 1.0}
        else:
            spot = set(rng.sample(pool, min(options.spot, len(pool))))
        run = sorted(cand | spot)
        est = sum((maps.vcpu[maps.index[id_of[n]]] or seconds[n]) for n in run)
        if est > options.max_cpu:
            print(f"{cid}: skipped, est {est:.0f}s", file=sys.stderr)
            detail[cid] = {"skipped": True, "est": est, "candidates": sorted(cand)}
            continue
        limits = {id_of[n]: max(900.0, 30 * seconds[n] + 300) for n in run}
        modules = []
        job = Job(cid, files, [id_of[n] for n in run], limits, modules, budget=10.0 ** 6)
        work.append((cid, job, cand, certain, spot, est))

    print(f"{len(work)} patches to label", file=sys.stderr)
    policy.preload({"tests/test_optimizer.py", "tests/test_executor.py", "tests/test_sqlglotc.py"}, lambda m: None)
    batch = []
    t0 = time.time()

    def flush():
        jobs = [b[1] for b in batch]
        run_jobs(jobs, repo, "/tmp" if os.path.isdir("/tmp") else ".", options.jobs, time.monotonic() + 10 ** 6,
                 log=lambda m: print(m, file=sys.stderr))
        for cid, job, cand, certain, spot, est in batch:
            if job.result is None or job.result.get("error"):
                detail[cid] = {"error": (job.result or {}).get("error", "killed"), "candidates": sorted(cand)}
                continue
            failed = {number_of[i] for i in job.result["failed"]}
            detail[cid] = {
                "failing": sorted(failed),
                "candidates": sorted(cand),
                "certain": sorted(certain),
                "spot": sorted(spot),
                "unsound": sorted(failed - cand),
                "cpu": round(job.cpu, 2),
                "est": round(est, 2),
            }
            flag = " UNSOUND " + str(sorted(failed - cand)) if failed - cand else ""
            print(f"{cid} {meta[cid]['kind']}: {len(failed)} failing of {len(cand)} candidates (+{len(spot)} spot), cpu {job.cpu:.1f}{flag}", file=sys.stderr)
        json.dump(detail, open(detail_path, "w"))
        batch.clear()

    for item in work:
        batch.append(item)
        if len(batch) >= 8:
            flush()
    if batch:
        flush()
    answers = {"commits": {}}
    for cid, d in detail.items():
        if "failing" in d:
            answers["commits"][cid] = {"failing": d["failing"], "kind": meta[cid]["kind"]}
    json.dump(answers, open(os.path.join(options.set_dir, "answers.json"), "w"))
    print(f"labeled {len(answers['commits'])} in {time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
