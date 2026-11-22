"""Decide, per commit, what to include outright and what to verify by running.

Costs are in seconds. Including a set S costs pack(S) (busiest worker + 2 s per
worker); verifying a test costs its CPU time in our container; missing a
failing test costs 300 s. Candidates are a sound superset, so verifying all of
them gives the exact answer; the policy only trades CPU against packing cost.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from .pack import pack
from .verify import Job, module_name, run_jobs

MISS = 300.0
# Fixed CPU cost of one verification child (fork, overlay, importing sqlglot
# and the test modules). Measured on the target image, see README.
VERIFY_OVERHEAD = float(os.environ.get("BR_VERIFY_OVERHEAD", "0.35"))
# Multiplier from the maps' per-test CPU measurements to the scoring machine.
CPU_SCALE = float(os.environ.get("BR_CPU_SCALE", "1.0"))
# Tests whose verification would cost more than this are never verified.
EXPENSIVE_CPU = float(os.environ.get("BR_EXPENSIVE_CPU", "3.0"))
# Never spend more than this on verifying one commit.
MAX_VERIFY_CPU = float(os.environ.get("BR_MAX_VERIFY_CPU", "20.0"))
PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
# Children whose patch touches the preloaded core keep the modules that cannot see the
# patch and import only the rest again (selector/verify.py _partial_refresh).
PARTIAL = os.environ.get("BR_PARTIAL", "1") == "1"
# Expensive candidates are left out only when verification shows the patch is
# inert: every changed expression was evaluated next to its original at least
# PROBE_MIN_COUNT times, by at least PROBE_MIN_TESTS cheap tests, and the two
# never differed, and no cheap test failed.
PROBE_MIN_COUNT = int(os.environ.get("BR_PROBE_MIN_COUNT", "50"))
# Infection probes leave out candidates that were not run (see README); off: "0".
PROBES = os.environ.get("BR_PROBES", "1") != "0"
PROBE_MIN_TESTS = int(os.environ.get("BR_PROBE_MIN_TESTS", "3"))
# Run fixture-loop tests restricted to the cases a patch can affect.
CASE_RESTRICT = os.environ.get("BR_CASE_RESTRICT", "1") == "1"
# Verify TPC-DS execution by comparing optimize() outputs (selector/stage.py).
STAGE = os.environ.get("BR_STAGE", "1") == "1"
STAGE_MISMATCH = 0.4  # rough chance a reached case's optimize() output differs

# Probability that one candidate test fails, by reason (measured on the kit
# sample and the labelled validation sets: ~0.13-0.26 for function-body edits,
# ~0.2 for import-time edits, every fixture reader in the sample failed); long
# tests (>= 5 s) fail far less often when they are candidates (TPC-DS: 2 of 27).
Q_FAIL = {"certain": 1.0, "data": 0.9, "pydata": 0.05, "func": 0.25, "imp": 0.2, "other": 0.3}
Q_BIG = float(os.environ.get("BR_Q_BIG", "0.2"))
EXPENSIVE_SHARE = float(os.environ.get("BR_EXPENSIVE_SHARE", "0.6"))

# Rough probability that a candidate set contains a failure, by reason.
# (Calibrated on the 30 sample commits: about 60% of code edits that reach any
# test break one, a quarter of class-level edits do, and every fixture edit did.)
P_FAIL = {
    "certain": 1.0,
    "data": 0.95,
    "pydata": 0.2,
    "func": 0.6,
    "imp": 0.35,
    "other": 0.5,
}


@dataclass
class Plan:
    cid: str
    include: set[int] = field(default_factory=set)
    verify: list[int] = field(default_factory=list)
    conditional: set[int] = field(default_factory=set)  # expensive tests, dropped if the patch proves inert
    lite_rest: set[int] = field(default_factory=set)  # cheap candidates a light probe left for a second round
    lite_rest_cpu: float = 0.0
    probes: dict = field(default_factory=dict)
    probe_result: dict | None = None
    files: dict = field(default_factory=dict)
    check_modules: list[str] = field(default_factory=list)
    import_fallback: dict = field(default_factory=dict)  # module -> numbers failing if it cannot import
    failed: set[int] | None = None
    import_errors: dict = field(default_factory=dict)
    verify_cpu_estimate: float = 0.0
    restrict: dict = field(default_factory=dict)  # number -> {fixture: case ordinals} for restricted runs
    stage: dict = field(default_factory=dict)  # number -> expected optimize() fingerprints (stage check)
    import_check: bool = False  # the changed modules must be imported to rule out import-time errors
    import_ok: bool = False  # that import check ran
    note: str = ""
    est: float | None = None  # the planner's expected cost of the chosen option (None: certain tests only)

    def final_tests(self) -> set[int]:
        chosen = set(self.include)
        if self.verify:
            if self.failed is None:
                # verification did not happen: include everything it was meant to decide
                chosen |= set(self.verify) | self.conditional
            else:
                chosen |= self.failed
                if self.conditional and not self.inert():
                    chosen |= self.conditional
        else:
            chosen |= self.conditional
        for module in self.import_errors:
            chosen |= set(self.import_fallback.get(module, ()))
        if self.import_check and not self.import_ok:
            # could not check that the changed modules still import: assume they do not
            for module in self.check_modules:
                chosen |= set(self.import_fallback.get(module, ()))
        return chosen


    def explain(self) -> dict:
        """number -> how it got into the final selection (dev diagnostics only)."""
        why = {}
        reason_of = getattr(self, "reason_of", {})
        expensive = getattr(self, "expensive", set())
        for n in self.include:
            why[n] = getattr(self, "include_tag", {}).get(n) or (
                "blind-" + ("exp-" if n in expensive else "") + reason_of.get(n, "other"))
        if self.verify:
            if self.failed is None:
                for n in set(self.verify) | self.conditional:
                    why.setdefault(n, "unverified")
            else:
                for n in self.failed:
                    why.setdefault(n, "verified-fail")
                if self.conditional and not self.inert():
                    for n in self.conditional:
                        why.setdefault(n, "conditional-" + ("exp" if n in expensive else "cheap"))
        else:
            for n in self.conditional:
                why.setdefault(n, "conditional")
        for module in self.import_errors:
            for n in self.import_fallback.get(module, ()):
                why.setdefault(n, "import-error")
        return why

    def inert(self) -> bool:
        if self.failed or self.include or self.import_errors or not self.probes or self.probe_result is None:
            return False
        expected = {f"{rel}:{k}" for rel, items in self.probes.items() for k in range(len(items))}
        stats = self.probe_result
        if "__incomplete__" in stats or set(stats) != expected:
            return False
        return all(v["infected"] == 0 and v["count"] >= PROBE_MIN_COUNT and v["tests"] >= PROBE_MIN_TESTS
                   for v in stats.values())


def probe_live_prior(item) -> float:
    """Prior that a probed edit changes some evaluation, from the kind of edit:
    `is` <-> `==` swaps are usually inert, ordering swaps sometimes, anything
    else (boolean operators, negations, constants) almost always changes values."""
    import ast

    old_src, new_src = item[0], item[3] if len(item) > 3 else ""
    try:
        old = ast.parse(old_src, mode="eval").body
        new = ast.parse(new_src, mode="eval").body
    except SyntaxError:
        return 0.8
    if isinstance(old, ast.Compare) and isinstance(new, ast.Compare) and len(old.ops) == len(new.ops) == 1 \
            and ast.dump(old.left) == ast.dump(new.left) and ast.dump(old.comparators[0]) == ast.dump(new.comparators[0]):
        pair = {type(old.ops[0]), type(new.ops[0])}
        if pair in ({ast.Is, ast.Eq}, {ast.IsNot, ast.NotEq}):
            return 0.25  # identity vs equality: differs only for odd __eq__
        if pair in ({ast.Lt, ast.LtE}, {ast.Gt, ast.GtE}):
            return 0.5  # differs only at the boundary
    return 0.9


def default_time_limit(n_commits: int) -> float:
    # 3 minutes for the practice batch (~40 commits), 4.5 for the final (~60).
    limit = 180.0 if n_commits <= 48 else 270.0
    return limit * 0.70


def cpu_estimate(maps, index: int, seconds: float) -> float:
    """CPU seconds our verifier needs for one test on the scoring machine."""
    measured = maps.cpu_estimate(index) if hasattr(maps, "cpu_estimate") else None
    if measured is not None:
        return measured * CPU_SCALE
    return max(seconds, 0.001) * 1.5 * CPU_SCALE


def case_restrictions(possible, found, maps, index_of, id_of_number) -> dict:
    """number -> {fixture: case ordinals} for candidates that only need some of
    their fixture cases run (see selector/cases.py). A test qualifies when every
    reason it is a candidate is attributable to cases:
      * code changes: the test has per-case coverage and the changed code is run
        by some restricted run (a case, or the code outside the loops, which every
        restricted run executes); the cases whose own coverage was hit are kept;
      * data changes: the changed file is one of the test's case fixtures; the
        cases that are new or whose text changed are kept.
    Anything else (no per-case coverage, another data file the test reads, a
    change no single-case run executes, tests/helpers.py touched) means no
    restriction: the test runs every case."""
    import os as _os

    from .cases import changed_new_cases

    STAGE_CHECKS.clear()
    if not maps.case_tests:
        return {}
    for f in found.files:
        # the loaders live in tests/helpers.py; other kinds of change (new,
        # deleted, renamed, binary files, pytest configuration) are not reasoned
        # about per case
        if f.path in ("tests/helpers.py", "tests/__init__.py") or f.kind not in ("py", "data", "mode"):
            return {}
    # One mask per reason a test can be a candidate: each Python change site, and
    # the per-file pydata / other / certain sets. A test is restricted only if
    # every reason that reaches it also reaches one of its restricted runs.
    reasons = []
    data = []
    for f in found.files:
        reasons.extend(m for m in f.site_masks if m)
        reasons.extend(m for m in (f.certain, f.pydata, f.other) if m)
        if f.kind == "data" and f.data:
            data.append(f)
        unattributed = f.func | f.imp
        for m in f.site_masks:
            unattributed &= ~m
        if unattributed:  # a site mask we did not record: be safe
            reasons.append(unattributed)
    code_mask = 0
    for m in reasons:
        code_mask |= m
    out = {}
    changed_cache = {}
    for n in possible:
        node = id_of_number.get(n)
        fixtures = maps.case_tests.get(node) if node else None
        if not fixtures or n not in index_of:
            continue
        bit = 1 << index_of[n]
        kinds = {_os.path.normpath(fx): kind for fx, kind in fixtures}
        need = {key: set() for key in kinds}
        ok = True
        if code_mask & bit:
            info = maps.cases.get(node)
            if not info or any(_os.path.normpath(fx) not in need for fx in info["fixtures"]):
                continue
            for m in reasons:
                if not m & bit:
                    continue
                hit = bool(m >> info["outside"] & 1)
                for fx, indexes in info["fixtures"].items():
                    key = _os.path.normpath(fx)
                    for k, pi in enumerate(indexes):
                        if m >> pi & 1:
                            need[key].add(k)
                            hit = True
                if not hit:
                    # this reason reaches the test but none of its restricted runs
                    ok = False
                    break
            if not ok:
                continue
        for f in data:
            if not f.data & bit:
                continue
            rel = f.path[len("tests/fixtures/"):] if f.path.startswith("tests/fixtures/") else None
            key = _os.path.normpath(rel) if rel else None
            if key is not None and key in {_os.path.normpath(x) for x in maps.case_also.get(node, ())}:
                # loaded by the class fixture but never consumed by this test: the
                # restricted run still loads (parses) the whole new file there
                continue
            if key not in need or f.old_text is None or f.new_text is None:
                ok = False
                break
            if (f.path, kinds[key]) not in changed_cache:
                changed_cache[(f.path, kinds[key])] = changed_new_cases(f.old_text, f.new_text, kinds[key])
            need[key] |= changed_cache[(f.path, kinds[key])]
        if ok:
            out[n] = need
            stage = _stage_check(maps, node, need, reasons, bit, any(f.data & bit for f in data))
            if stage is not None:
                STAGE_CHECKS[n] = stage
    return out


STAGE_CHECKS: dict = {}  # number -> (expected fingerprint triples in call order, CPU estimate); filled per commit


def _stage_check(maps, node, need, reasons, bit, data_hit):
    """Expected optimize() fingerprints if `node` can be verified by stopping each
    needed case after optimize() (selector/stage.py), else None. Requires: no data
    change the test reads, no reason that reaches code the test runs outside
    optimize() (any case's outer pseudo-test, or the code outside the case loop),
    and deterministic recordings with at least one call for every needed case."""
    import os as _os

    info = getattr(maps, "stage", {}).get(node)
    cases_info = maps.cases.get(node)
    if not info or not cases_info or data_hit:
        return None
    outside = cases_info["outside"]
    outer_mask = 0
    for i in info["outer"]:
        outer_mask |= 1 << i
    for m in reasons:
        if m & bit and (m & outer_mask or m >> outside & 1):
            return None
    keep = sorted(need.get(_os.path.normpath(info["fixture"]), ()))
    if any(len(v) for fx, v in need.items() if fx != _os.path.normpath(info["fixture"])):
        return None
    if not keep:
        return None
    per_case = []
    warm = info.get("warm", 0.0)
    cpu = warm  # each case was measured alone; run together they share one warm-up
    for k in keep:
        calls = info["calls"][k] if k < len(info["calls"]) else None
        if not calls:
            return None
        per_case.append((k, [tuple(c) for c in calls]))
        cpu += max(0.0, (info["cpu"][k] or 0.0) - warm)
    return per_case, cpu


def case_full_costs(maps, node):
    """(warm-up, {ordinal: marginal CPU}) of running cases of `node` in full, from
    the per-case measurements (as in restricted_estimate), or None."""
    info = maps.cases.get(node)
    if not info or len(info["fixtures"]) != 1:
        return None
    vcpu = maps.vcpu
    outside = vcpu[info["outside"]]
    (indexes,) = info["fixtures"].values()
    whole = vcpu[maps.index[node]] if node in maps.index else None
    per_case = [vcpu[i] for i in indexes]
    if outside is None or whole is None or None in per_case or len(per_case) < 2:
        return None
    marginal = [max(0.0, v - outside) for v in per_case]
    warm = max(0.0, (sum(marginal) - max(0.0, whole - outside)) / (len(marginal) - 1))
    return outside + warm, {k: max(0.0, m - warm) for k, m in enumerate(marginal)}


STATE_CHECK_CPU = float(os.environ.get("BR_STATE_CHECK_CPU", "0.65"))  # local CPU s of one comparison (0.60-0.70 measured)
# Scorer CPU per local CPU second for this work: practice M13 -> M17 cost +9.4 s of
# scorer CPU for about +5 s of local comparison CPU per 40 official-style commits.
STATE_CPU_SCALE = float(os.environ.get("BR_STATE_CPU_SCALE", "1.9"))


def _optimistic_masks(found, maps, finder) -> dict:
    """{(report, site index): mask} the state comparison would leave if it confirms
    that only the edited value changed: key records of the edited keys (plus
    enumerators and tests without a record), the module sites' record alternative, the
    runtime readers of the assigned class attributes. Sites without an estimate are
    left out (their masks stay as they are)."""
    rec = getattr(maps, "keyrec", None) or {}
    lookups = rec.get("lookups", {})
    holders = set(rec.get("holders", ())) - set(rec.get("unrecordable", ()))
    untraced = maps.all_mask & ~maps.mask(rec.get("traced"))
    out = {}
    for f in found.files:
        for i, (holder, keys) in getattr(f, "keyrec_sites", {}).items():
            if holder not in holders:
                continue
            by_key = lookups.get(holder, {})
            m = maps.mask(by_key.get("*")) | untraced
            ok = True
            for desc in keys:
                if desc[0] == "str":
                    m |= maps.mask(by_key.get(desc[1]))
                elif desc[0] in ("attr", "name"):
                    name = desc[1]
                    for k in by_key:
                        if (k.startswith("enum:") and k.endswith("." + name)) or \
                                (k.startswith("class:") and (k.endswith(":" + name) or k.endswith("." + name))):
                            m |= maps.mask(by_key[k])
                else:
                    ok = False
                    break
            if ok:
                out[(id(f), i)] = (f, f.site_masks[i] & m)
        for i, (alt, _group) in getattr(f, "modrec_sites", {}).items():
            if (id(f), i) not in out:
                out[(id(f), i)] = (f, f.site_masks[i] & alt)
        if finder is not None:
            for i, (holder, names) in getattr(f, "classrec_sites", {}).items():
                if (id(f), i) in out:
                    continue
                m = 0
                for name in names:
                    r = finder.runtime_attr_mask(holder, name)
                    if r is None:
                        m = None
                        break
                    m |= r
                if m is not None:
                    out[(id(f), i)] = (f, f.site_masks[i] & m)
    return out


def state_check_worthwhile(cid, changes, found, maps, map_to_number, seconds_of, unknown_numbers, finder=None,
                           log=None) -> bool:
    """Run the state comparison (selector/keyrefine.py) only when it pays: plan the
    commit as it stands and with the masks a confirming comparison would leave
    (computed here without fingerprints), and require the planner's expected cost to
    drop by more than the comparison's scorer CPU. Skipping only keeps the wider
    masks, so it never loses soundness; the comparison itself still decides what is
    narrowed."""
    optimistic = _optimistic_masks(found, maps, finder)
    if not optimistic:
        return False
    ms_of = {n: int(seconds_of[n] * 1000 + 0.5) for n in seconds_of}

    def planned() -> float:
        plan = plan_commit(cid, changes, found, maps, map_to_number, seconds_of, unknown_numbers)
        if plan.est is not None:
            return plan.est
        return pack(sorted(plan.include), ms_of, fast=True)[1] / 1000.0

    now = planned()
    saved = [(f, list(f.site_masks), f.imp) for f in found.files]
    try:
        for (fid, i), (f, mask) in optimistic.items():
            f.site_masks[i] = mask
        for f in found.files:
            if any(fid == id(f) for fid, _ in optimistic):
                imp = 0
                for j in getattr(f, "imp_sites", []):
                    imp |= f.site_masks[j]
                f.imp = imp
        after = planned()
    finally:
        for f, masks, imp in saved:
            f.site_masks[:] = masks
            f.imp = imp
    threshold = STATE_CHECK_CPU * STATE_CPU_SCALE
    if log is not None:
        log(f"{cid}: state comparison would save {now - after:.2f}s ({now:.2f} -> {after:.2f}) "
            f"for {threshold:.2f}s scorer CPU")
    return now - after > threshold

def restricted_estimate(maps, node, need, full_est) -> float:
    """CPU of a run restricted to `need`: the code outside the loops plus each
    kept case (per-case measurements when we have them, else a share of the
    whole test's cost)."""
    import os as _os

    info = maps.cases.get(node)
    if info:
        vcpu = maps.vcpu
        outside = vcpu[info["outside"]]
        per_case = [vcpu[i] for indexes in info["fixtures"].values() for i in indexes]
        whole = vcpu[maps.index[node]] if node in maps.index else None
        if outside is not None and whole is not None and None not in per_case and len(per_case) > 1:
            # Each case was measured alone, cold; run together they share one
            # warm-up w, fitted so that all cases together cost the whole test.
            marginal = [max(0.0, v - outside) for v in per_case]
            warm = max(0.0, (sum(marginal) - max(0.0, whole - outside)) / (len(marginal) - 1))
            total = outside
            kept = 0
            for fx, indexes in info["fixtures"].items():
                for k in need.get(_os.path.normpath(fx), ()):
                    if k < len(indexes):
                        total += max(0.0, vcpu[indexes[k]] - outside - warm)
                        kept += 1
            if kept:
                total += warm
            return min(full_est, (total + 0.02) * CPU_SCALE)
    cases_total = sum(maps.case_counts.get(fx, 0) for fx, _ in maps.case_tests.get(node, ()))
    kept = sum(len(v) for v in need.values())
    if not cases_total:
        return full_est
    return min(full_est, full_est * kept / cases_total + 0.05)


def plan_commit(cid, changes, found, maps, map_to_number, seconds_of, unknown_numbers) -> Plan:
    plan = Plan(cid)

    def numbers(mask):
        out = set()
        for i in maps.tests_of(mask):
            n = map_to_number[i]
            if n is not None:
                out.add(n)
        return out

    if found.unknown:
        plan.include = set(seconds_of)
        plan.include_tag = {n: "unknown-all" for n in plan.include}
        plan.note = "unknown change: everything"
        return plan

    certain = numbers(found.certain)
    plan.include_tag = {n: "certain" for n in certain}
    by_reason = {"data": 0, "pydata": 0, "func": 0, "imp": 0, "other": 0}
    for f in found.files:
        by_reason["data"] |= f.data
        by_reason["pydata"] |= f.pydata
        by_reason["func"] |= f.func
        by_reason["imp"] |= f.imp
        by_reason["other"] |= f.other
    possible = numbers(found.possible) - certain
    if unknown_numbers and possible:
        # Tests we have no coverage for: cannot rule them out.
        possible |= set(unknown_numbers)
    plan.include = set(certain)
    if not possible:
        plan.note = "nothing reachable" if not certain else "certain only"
        _prepare_checks(plan, changes, found, maps, numbers, flagged_only=True)
        if plan.import_check:
            plan.note += " | import check"
        return plan

    reason_of = {}
    for reason in ("data", "func", "imp", "pydata", "other"):
        for n in numbers(by_reason[reason]):
            reason_of.setdefault(n, reason)
    index_of = {n: i for i, n in enumerate(map_to_number) if n is not None}
    id_of_number = {n: maps.tests[i] for n, i in index_of.items()}
    est = {n: cpu_estimate(maps, index_of[n], seconds_of[n]) if n in index_of else seconds_of[n] * 3 + 1
           for n in possible}
    ms_of = {n: int(seconds_of[n] * 1000 + 0.5) for n in seconds_of}
    if CASE_RESTRICT:
        try:
            restrict = case_restrictions(possible, found, maps, index_of, id_of_number)
        except Exception:  # noqa: BLE001 - never needed for soundness: run every case
            restrict = {}
        for n, need in restrict.items():
            reduced = restricted_estimate(maps, id_of_number[n], need, est[n])
            if reduced < est[n]:
                est[n] = reduced
                plan.restrict[n] = need
            if STAGE and n in STAGE_CHECKS:
                per_case, stage_cpu = STAGE_CHECKS[n]
                stage_est = (stage_cpu + 0.05) * CPU_SCALE
                # A different optimize() result does not mean the test fails: the
                # cases whose result differs are then run in full, when that stays
                # under what it is worth (otherwise the test is included).
                cap = max(EXPENSIVE_CPU, EXPENSIVE_SHARE * seconds_of[n]) / CPU_SCALE
                costs = case_full_costs(maps, id_of_number[n])
                if costs is not None:
                    stage_est += STAGE_MISMATCH * min(reduced, cap * CPU_SCALE)
                if stage_est < est[n]:
                    est[n] = stage_est
                    plan.restrict[n] = need
                    plan.stage[n] = {"fixture": maps.stage[id_of_number[n]]["fixture"], "cases": per_case,
                                     "cap": cap if costs is not None else 0.0,
                                     "warm": costs[0] if costs else 0.0, "full": costs[1] if costs else {}}

    # Too costly to run here: more CPU than EXPENSIVE_CPU and more than
    # EXPENSIVE_SHARE of the test's recorded seconds (what including it costs).
    expensive = {n for n in possible if est[n] > max(EXPENSIVE_CPU, EXPENSIVE_SHARE * seconds_of[n])}
    cheap = possible - expensive
    plan.reason_of = reason_of
    plan.expensive = expensive
    reasons = {reason_of.get(n, "other") for n in possible}
    p_any = max((P_FAIL[r] for r in reasons), default=0.0)

    pack_cache = {}

    def packed(tests) -> float:
        key = frozenset(tests)
        if key not in pack_cache:
            pack_cache[key] = pack(sorted(tests), ms_of, fast=True)[1] / 1000.0
        return pack_cache[key]

    def verify_cpu_of(tests) -> float:
        classes = {"::".join(id_of_number[n].split("::")[:2]) for n in tests if n in id_of_number}
        return VERIFY_OVERHEAD + sum(est[n] for n in tests) + sum(maps.setup_cost.get(c, 0.0) for c in classes)

    q_of = {}
    for n in possible:
        q = Q_FAIL.get(reason_of.get(n, "other"), 0.3)
        q_of[n] = min(q, Q_BIG) if seconds_of[n] >= 5.0 else q

    def expected(blind, verify):
        """Expected cost of including `blind` outright and running `verify`:
        CPU of the run, plus packing what is included and what fails. Long
        verified tests (>= 1 s recorded) are priced one by one (failing, they
        must be included after all); short ones only matter through whether
        any fails at all (a new worker if nothing else is included)."""
        base = certain | blind
        if not verify:
            return packed(base), 0.0
        cpu = verify_cpu_of(verify)
        c0 = packed(base) if base else 0.0
        longest = sorted((n for n in verify if seconds_of[n] >= 1.0), key=lambda n: -seconds_of[n])
        short = [n for n in verify if seconds_of[n] < 1.0] + longest[6:]
        none_fail = 1.0
        for n in short:
            none_fail *= 1.0 - q_of.get(n, 0.3)
        p_short = min(p_any, 1.0 - none_fail)
        if base:
            after = c0 + p_short * 0.3
        else:
            times = sorted(seconds_of[n] for n in short) or [0.0]
            after = p_short * (2.0 + times[len(times) // 2])
        for n in longest[:6]:
            extra = (packed(base | {n}) - c0) if base else 2.0 + seconds_of[n]
            after += q_of.get(n, 0.3) * max(0.0, extra)
        return cpu + after, cpu

    options = [("blind", possible, set())]
    if cheap:
        options.append(("verify", expensive, cheap))
        # Tests that cost far more to run here than to include (process-pool
        # tests run serially by us) go in outright; verify the rest.
        heavy = {n for n in cheap if est[n] > 1.5 * seconds_of[n] + 0.05}
        if heavy and cheap - heavy:
            options.append(("mixed", expensive | heavy, cheap - heavy))
        # Verify only the tests that save the most recorded time per CPU second
        # (long recorded, cheap to run here) and include the cheap tail outright.
        ranked = sorted(cheap, key=lambda n: (-(seconds_of[n] + 0.001) / (est[n] + 0.01), n))
        m = 1
        while m < len(ranked):
            options.append((f"top{m}", possible - set(ranked[:m]), set(ranked[:m])))
            m = m * 2 if m < 4 else int(m * 1.6)
    # Probe option: every change is a side-effect-free expression in a function
    # body and nothing else is touched; expensive tests become conditional.
    probe_items = {}
    probe_ok = bool(expensive) and bool(cheap) and not certain
    for f in found.files:
        if f.imp or f.data or f.pydata or f.other or f.certain:
            probe_ok = False
        for site in f.sites or []:
            if site.kind == "docstring":
                continue
            if site.scope != "function" or not site.probes:
                probe_ok = False
                continue
            probe_items.setdefault(f.path, []).extend(site.probes)
    lite_verify = set()
    if not PROBES:
        probe_items = {}
    if probe_items and all(not (f.imp or f.data or f.pydata or f.other or f.certain) for f in found.files) and not certain:
        all_function = all(site.scope == "function" and site.probes for f in found.files for site in (f.sites or []) if site.kind != "docstring")
        if all_function and cheap and max(probe_live_prior(i) for items in probe_items.values() for i in items) <= 0.25:
            # Edits that are usually inert (`is` <-> `==`): run only the cheapest
            # few candidates to watch the edited expressions; if they never
            # differ from the originals, nothing can fail.
            def class_of(n):  # noqa: E306
                return "::".join(id_of_number.get(n, "").split("::")[:2])

            def lite_cost(n):
                return est[n] + maps.setup_cost.get(class_of(n), 0.0)

            budget = 0.0
            paid = set()
            for n in sorted(cheap, key=lambda n: (lite_cost(n), n)):
                if len(lite_verify) >= 3 and budget >= 0.6:
                    break
                lite_verify.add(n)
                budget += est[n] + (0.0 if class_of(n) in paid else maps.setup_cost.get(class_of(n), 0.0))
                paid.add(class_of(n))
            if len(lite_verify) < len(cheap) or expensive:
                options.append(("probe-lite", set(), lite_verify))
    if probe_ok and probe_items:
        options.append(("probe", set(), cheap))
    scored = []
    for name, blind, verify in options:
        cost, cpu = expected(blind, verify)
        if name == "probe":
            live = max(probe_live_prior(item) for items in probe_items.values() for item in items)
            cost = cpu + live * (packed(expensive) + p_any * 0.5) + (1 - live) * p_any * 2.5
        elif name == "probe-lite":
            live = max(probe_live_prior(item) for items in probe_items.values() for item in items)
            cost = cpu + live * packed(possible)
        if cpu <= MAX_VERIFY_CPU:
            scored.append((cost, name, blind, verify, cpu))
    scored.sort(key=lambda x: x[0])
    cost, name, blind, verify, cpu = scored[0]
    plan.est = cost
    summary = ", ".join(f"{n}={c:.2f}" for c, n, *_ in scored)
    plan.include |= blind
    if not verify:
        plan.note = f"blind {len(possible)} ({summary})"
        _prepare_checks(plan, changes, found, maps, numbers, flagged_only=True)
        if plan.import_check:
            plan.note += " | import check"
        return plan
    plan.verify = sorted(verify)
    plan.verify_cpu_estimate = cpu
    if name == "probe":
        plan.conditional = set(expensive)
        plan.probes = probe_items
    elif name == "probe-lite":
        plan.conditional = possible - verify
        plan.probes = probe_items
        plan.lite_rest = cheap - verify
        plan.lite_rest_cpu = verify_cpu_of(plan.lite_rest) if plan.lite_rest else 0.0
    plan.note = f"{name}: verify {len(verify)}, include {len(blind)} ({summary})"
    # keep the restrictions of every test that may be run: this round, and the
    # second round of a light probe (plan.lite_rest)
    plan.restrict = {n: plan.restrict[n] for n in set(verify) | plan.lite_rest if n in plan.restrict}
    plan.stage = {n: plan.stage[n] for n in plan.restrict if n in plan.stage}
    restricted = {n: sum(len(v) for v in plan.restrict[n].values()) for n in verify if n in plan.restrict}
    if restricted:
        plan.note += " | cases " + ", ".join(
            f"{id_of_number[n].rsplit('::', 1)[-1]}={k}({est[n]:.1f}s{', stage' if n in plan.stage else ''})"
            for n, k in sorted(restricted.items()))

    # Files for the overlay and modules whose import we double-check.
    _prepare_checks(plan, changes, found, maps, numbers, flagged_only=False)
    return plan


def _prepare_checks(plan, changes, found, maps, numbers, flagged_only):
    """Overlay files for a verification child, and the modules it imports first
    (an import that fails means every importer fails). flagged_only: only the
    modules whose change runs at import time and might raise there."""
    flagged = {f.path for f in found.files if getattr(f, "import_check", False)}
    if flagged_only and not flagged:
        return
    if flagged:
        plan.import_check = True
    for change in changes:
        report = next((f for f in found.files if f.path == change.path), None)
        if change.old_path and change.is_rename:
            plan.files[change.old_path] = None
        if change.is_deleted:
            plan.files[change.old_path] = None
            continue
        if report is not None and report.new_text is not None:
            plan.files[change.path] = report.new_text.encode("utf-8")
        name = module_name(change.path)
        if not name or not (change.path.startswith("sqlglot/") or change.path.startswith("tests/")):
            continue
        if flagged_only and change.path not in flagged:
            continue
        if name not in plan.check_modules:
            plan.check_modules.append(name)
        imp_mask = maps.import_mask(change.old_path or change.path)
        plan.import_fallback[name] = numbers(imp_mask)


_MAPS = None  # set by main for verify_groups (opens map)


def verify_groups(plan: Plan, numbers, id_of):
    """Split a verification into a restricted group (fixture-loop tests run on
    some cases) and an unrestricted group. A test only joins the restricted group
    if every restricted fixture it reads is one of its own case fixtures, so the
    loader wrapper never trims a read we did not reason about."""
    import os as _os

    if _MAPS is None:
        return None
    # Stage-checked tests (selector/stage.py) run alone: the optimize() hook would
    # stop any other test that executes queries.
    staged = [n for n in numbers if n in plan.stage and n in plan.restrict]
    stage_groups = [({fx: sorted(v) for fx, v in plan.restrict[n].items()}, [id_of[n]], plan.stage[n]) for n in staged]
    numbers = [n for n in numbers if n not in staged]
    restricted = {n: plan.restrict[n] for n in numbers if n in plan.restrict}
    if not restricted:
        if not stage_groups:
            return None
        return ([({}, [id_of[n] for n in numbers])] if numbers else []) + stage_groups
    while True:
        allowed = {}
        for n, need in restricted.items():
            for fx, keep in need.items():
                allowed.setdefault(fx, set()).update(keep)
        moved = False
        for n in list(restricted):
            node = id_of[n]
            own = {_os.path.normpath(fx) for fx, _ in _MAPS.case_tests.get(node, ())}
            own |= {_os.path.normpath(fx) for fx in _MAPS.case_also.get(node, ())}
            index = _MAPS.index.get(node)
            for fx in allowed:
                if fx in own:
                    continue
                if index is None or _MAPS.open_mask("tests/fixtures/" + fx) >> index & 1:
                    del restricted[n]
                    moved = True
                    break
        if not moved:
            break
    others = [id_of[n] for n in numbers if n not in restricted]
    # Unrestricted tests that read a restricted fixture through a loader would be
    # trimmed too if they shared the group: they run in their own group.
    groups = []
    if others:
        groups.append(({}, others))
    if restricted:
        groups.append(({fx: sorted(v) for fx, v in allowed.items()}, [id_of[n] for n in sorted(restricted)]))
    return (groups + stage_groups) or None


def verify_plans(plans: list[Plan], root: str, tmp: str, deadline: float, log, id_of, number_of, seconds_of) -> None:

    jobs = []
    by_key = {}
    for plan in plans:
        if not plan.verify and not plan.import_check:
            continue
        # tests.json numbers -> node ids through the plan's own mapping
        jobs.append(plan)
    if not jobs:
        return
    work = []
    for plan in jobs:
        ids = [id_of[n] for n in plan.verify]
        limits = {id_of[n]: max(10.0, 6.0 * seconds_of[n] + 5.0) for n in plan.verify}
        budget = max(30.0, 4.0 * plan.verify_cpu_estimate + 15.0)
        groups = verify_groups(plan, plan.verify, id_of)
        job = Job(plan.cid, plan.files, ids, limits, plan.check_modules, budget, probes=plan.probes or None,
                  groups=groups)
        by_key[plan.cid] = (plan, job)
        work.append(job)
    preload(set(t.split("::", 1)[0] for job in work for t in job.tests) | {m for job in work for m in job.check_modules}, log)
    loaded = preload_sqlglot(root, log)
    for job in work:
        touched = {module_name(p) for p in job.files if p.endswith(".py")}
        job.warm = bool(loaded) and not (touched & loaded)
        if PARTIAL and loaded and not job.warm and not job.probes:
            job.partial = sorted(touched & loaded)
    # Cheapest first: most value per second if time runs short.
    work.sort(key=lambda j: by_key[j.key][0].verify_cpu_estimate)
    run_jobs(work, root, tmp, PARALLEL, deadline, log=log)
    for plan, job in by_key.values():
        plan.verify_cpu = job.cpu
        if job.result is None or job.result.get("error"):
            plan.failed = None
            plan.note += " | verification unavailable"
            continue
        plan.import_ok = True
        plan.failed = {number_of[i] for i in job.result["failed"] if i in number_of}
        plan.import_errors = job.result.get("import_errors", {})
        if job.result.get("stage"):
            plan.note += f" | stage {job.result['stage']}"
        if plan.probes:
            plan.probe_result = job.result.get("probes", {})
            plan.note += f" | probes {plan.probe_result} -> {'inert' if plan.inert() else 'live'}"
        plan.note += f" | ran {len(job.tests)} in {job.cpu:.2f}s cpu: {len(plan.failed)} failed"

    # Second round for light probes that did not prove the edit inert: run the
    # remaining cheap candidates too (exact answers for them instead of
    # including them blindly; more evaluations if the probe was inconclusive).
    second = []
    for plan, job in by_key.values():
        stats = plan.probe_result
        if not plan.lite_rest or plan.failed is None or plan.import_errors or stats is None:
            continue
        if plan.inert() or plan.lite_rest_cpu > MAX_VERIFY_CPU:
            continue
        live = bool(plan.failed) or "__incomplete__" in stats or any(v["infected"] for v in stats.values())
        if live:
            expensive_left = plan.conditional - plan.lite_rest
            if expensive_left:
                continue  # expensive tests go in anyway; the rest rides along cheaply
            rest_ms = {n: int(seconds_of[n] * 1000 + 0.5) for n in plan.lite_rest}
            if plan.lite_rest_cpu >= pack(sorted(plan.lite_rest), rest_ms)[1] / 1000.0:
                continue  # verifying the rest would cost more than including it
        rest = sorted(plan.lite_rest)
        ids = [id_of[n] for n in rest]
        limits = {id_of[n]: max(10.0, 6.0 * seconds_of[n] + 5.0) for n in rest}
        job2 = Job(plan.cid + "-2", plan.files, ids, limits, [], max(30.0, 4.0 * plan.lite_rest_cpu + 15.0),
                   probes=plan.probes, groups=verify_groups(plan, rest, id_of))
        touched = {module_name(p) for p in job2.files if p.endswith(".py")}
        job2.warm = bool(loaded) and not (touched & loaded)
        second.append((plan, job2))
    if second:
        run_jobs([j for _, j in second], root, tmp, PARALLEL, deadline, log=log)
        for plan, job2 in second:
            if job2.result is None or job2.result.get("error"):
                plan.note += " | second round unavailable"
                continue  # conditional tests stay included
            failed2 = {number_of[i] for i in job2.result["failed"] if i in number_of}
            plan.verify_cpu = getattr(plan, "verify_cpu", 0.0) + job2.cpu
            plan.failed |= failed2
            plan.conditional -= plan.lite_rest
            merged = {k: dict(v) for k, v in plan.probe_result.items()}
            for k, v in job2.result.get("probes", {}).items():
                m = merged.setdefault(k, {"count": 0, "infected": 0, "tests": 0})
                m["count"] += v["count"]
                m["infected"] += v["infected"]
                m["tests"] += v["tests"]
            plan.probe_result = merged
            plan.note += (f" | round 2 ran {len(job2.tests)} in {job2.cpu:.2f}s cpu: {len(failed2)} failed,"
                          f" probes {merged} -> {'inert' if plan.inert() else 'live'}")


CORE_MODULES = (
    "sqlglot", "sqlglot.optimizer", "sqlglot.executor", "sqlglot.dialects.bigquery",
    "sqlglot.dialects.hive", "sqlglot.dialects.snowflake", "sqlglot.dialects.spark2",
)


def preload_sqlglot(root: str, log) -> set[str]:
    """Import the pinned sqlglot core once in the parent. A child whose patch
    touches none of these modules reuses them (re-pointed at its overlay);
    any other child drops them and imports everything from its overlay."""
    import importlib

    from . import verify as _verify

    sys_path = list(sys.path)
    sys.path.insert(0, root)
    watch = _verify.ImportWatch() if PARTIAL else None
    if watch is not None:
        sys.meta_path.insert(0, watch)
    try:
        for name in CORE_MODULES:
            importlib.import_module(name)
    except Exception as error:  # noqa: BLE001
        log(f"core preload failed: {error!r}")
        for name in list(sys.modules):
            if name == "sqlglot" or name.startswith("sqlglot."):
                del sys.modules[name]
        return set()
    finally:
        sys.path[:] = sys_path
        if watch is not None:
            sys.meta_path.remove(watch)
    return {name for name in sys.modules if name == "sqlglot" or name.startswith("sqlglot.")}


def preload(files: set[str], log) -> None:
    """Load third-party libraries once in the parent so children inherit them."""
    import importlib

    from . import shim

    names = ["unittest", "unittest.mock", "json", "datetime", "decimal", "re", "typing", "itertools",
             "collections", "functools", "logging", "copy", "math", "enum", "dataclasses", "string"]
    heavy = any(("test_optimizer" in f or "test_executor" in f) for f in files)
    if heavy:
        names += ["numpy", "pandas", "pandas.testing", "duckdb", "concurrent.futures", "multiprocessing", "csv", "gzip"]
    if any("test_sqlglotc" in f for f in files):
        names += ["setuptools", "setuptools.command.build_ext", "runpy", "tempfile"]
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as error:  # noqa: BLE001
            log(f"preload {name}: {error!r}")
    shim.install()
