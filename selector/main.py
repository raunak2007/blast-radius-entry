"""Blast Radius selector: for each patch, print which tests to run and how to split them.

Pipeline per commit:
  1. parse the patch and apply it in memory to the pinned sqlglot checkout;
  2. find a sound superset of tests that could fail (selector.candidates), using
     per-test coverage maps built from the pinned commit;
  3. when it is cheaper in expectation, actually run the cheap candidates
     against the patched tree (selector.verify) and keep only those that fail;
  4. pack the final tests onto workers (selector.pack).
Only answer lines go to stdout; everything else goes to stderr.
"""

from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from selector import keyrefine, policy  # noqa: E402
from selector.candidates import CandidateFinder  # noqa: E402
from selector.maps import Maps  # noqa: E402
from selector.pack import pack  # noqa: E402
from selector.patch import parse_patch  # noqa: E402

START = time.monotonic()
DATA = os.environ.get("BR_DATA", "/data")
ROOT = os.environ.get("BR_ROOT", "/opt/sqlglot")
TMP = os.environ.get("BR_TMP", "/tmp")
TIME_LIMIT = float(os.environ.get("BR_TIME_LIMIT", "0") or 0)


def log(message: str):
    print(f"[{time.monotonic() - START:6.2f}] {message}", file=sys.stderr, flush=True)


def main():
    tests = json.loads(open(os.path.join(DATA, "tests.json")).read())["tests"]
    number_of = {t["id"]: t["number"] for t in tests}
    ms_of = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    seconds_of = {t["number"]: t["seconds"] for t in tests}
    commit_dir = os.path.join(DATA, "commits")
    commits = sorted(name[:-6] for name in os.listdir(commit_dir) if name.endswith(".patch"))
    log(f"{len(tests)} tests, {len(commits)} commits")

    answers: dict[str, list[int]] = {}
    # Fallback first: if anything below crashes we still print valid lines.
    try:
        maps = Maps()
        policy._MAPS = maps
        finder = CandidateFinder(maps, ROOT)
        # Map map indexes to this run's test numbers (ids not in our maps are unknown).
        map_to_number = [number_of.get(node) for node in maps.tests]
        unknown_numbers = sorted(set(number_of.values()) - {n for n in map_to_number if n is not None})
        if unknown_numbers:
            log(f"{len(unknown_numbers)} tests are not in the maps; they are added whenever a commit is risky")

        total_limit = TIME_LIMIT or policy.default_time_limit(len(commits))
        # Analysis is not interruptible like verification: past this budget the
        # remaining commits are not analysed and run everything (never reached in
        # normal batches: analysis takes ~0.1 s per commit).
        analysis_deadline = START + total_limit * 0.5
        plans = []
        for cid in commits:
            if time.monotonic() > analysis_deadline:
                log(f"{cid}: analysis time budget spent; running everything")
                plans.append(policy.Plan(cid, include=set(number_of.values())))
                continue
            try:
                text = open(os.path.join(commit_dir, cid + ".patch"), "rb").read().decode("utf-8", "replace")
                changes = parse_patch(text)
                paths = [c.path for c in changes]
                if (text.strip() and not changes) or len(set(paths)) != len(paths):
                    # text we could not read as a diff, or two sections for one file:
                    # do not reason about it
                    raise ValueError("unparsed patch or repeated file section")
                found = finder.analyze(changes)
                key_note = ""
                if not found.unknown and any(getattr(f, "keyrec_sites", None) or getattr(f, "modrec_sites", None)
                                             or getattr(f, "classrec_sites", None) for f in found.files) \
                        and time.monotonic() - START < total_limit * 0.3 \
                        and policy.state_check_worthwhile(cid, changes, found, maps, map_to_number, seconds_of,
                                                     unknown_numbers, finder, log):
                    try:
                        # never let the fingerprints eat into the analysis budget
                        budget = START + total_limit * 0.4 - time.monotonic()
                        key_note = keyrefine.refine(found, changes, ROOT, TMP, maps, log,
                                                    timeout=max(5.0, min(60.0, budget)), finder=finder)
                    except Exception as error:  # noqa: BLE001 - only a narrowing: keep the masks
                        key_note = f"keys: failed ({error!r})"
                plan = policy.plan_commit(cid, changes, found, maps, map_to_number, seconds_of, unknown_numbers)
                if key_note:
                    plan.note = (plan.note + " | " + key_note) if plan.note else key_note
            except Exception as error:  # noqa: BLE001 - never lose a commit
                log(f"{cid}: analysis failed ({error!r}); running everything")
                plan = policy.Plan(cid, include=set(number_of.values()))
            plans.append(plan)

        deadline = START + total_limit
        id_of = {t["number"]: t["id"] for t in tests}
        policy.verify_plans(plans, ROOT, TMP, deadline, log, id_of, number_of, seconds_of)
        dump = os.environ.get("BR_PLAN_DUMP")
        for plan in plans:
            answers[plan.cid] = sorted(plan.final_tests())
            log(f"{plan.cid}: {len(answers[plan.cid])} tests; {plan.note}")
        if dump:  # dev diagnostics only (never set in the image)
            with open(dump, "w") as handle:
                for plan in plans:
                    handle.write(json.dumps({"commit": plan.cid, "why": {str(k): v for k, v in plan.explain().items()},
                                             "note": plan.note, "verify_cpu": getattr(plan, "verify_cpu", 0.0),
                                             "verify_est": plan.verify_cpu_estimate,
                                             "lite_rest_est": plan.lite_rest_cpu}) + "\n")
    except Exception as error:  # noqa: BLE001
        log(f"fatal: {error!r}; falling back to running everything")
        for cid in commits:
            answers.setdefault(cid, [t["number"] for t in tests])

    total_ms = 0
    out = []
    for cid in commits:
        selected = answers.get(cid, [t["number"] for t in tests])
        workers, cost = pack(selected, ms_of)
        total_ms += cost
        out.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    log(f"done: simulated cost of selections {total_ms / 1000:.3f}s")
    sys.stderr.flush()
    # Skip interpreter teardown (it costs CPU, and native extensions loaded for
    # the verifier print leak reports at exit).
    os._exit(0)


if __name__ == "__main__":
    main()
