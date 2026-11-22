"""Label a validation set by running real pytest on each patched tree (dev tool).

Independent of the selector's narrowing: the tests run for a patch are a coarse
superset chosen from the pinned commit's maps only by *where* the patch edits:

* an edit strictly inside function bodies (sqlglot or test code): every test that
  entered one of the enclosing top-level functions/methods, or ran any line of them;
* any other edit to a .py file (module level, class body, decorators, defaults,
  signatures): every test that imported the module or read the file;
* data files: every test that read the file, or listed its directory;
* new .py files, deleted/renamed files, pytest/test-helper configuration: everything.

Plus a random sample of other importers (spot checks). Tests run with real
pytest (real spawn process pools, pytest 9 subtests, pytest-timeout with a
generous limit) in an overlay of the pinned checkout with the patch applied.

Usage: python tools/label_pytest.py <set dir> <tests.json> <sqlglot checkout> [--jobs 2] [--spot 30]
       [--only id,id] [--timeout 1500]
Writes <set dir>/label_detail.json (incremental) and <set dir>/answers.json.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import random
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from selector.maps import Maps  # noqa: E402
from selector.patch import apply_hunks, changed_old_lines, parse_patch  # noqa: E402
from selector.verify import build_overlay  # noqa: E402

CONFIG = {"pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "conftest.py"}


def function_spans(source: str):
    """(body_start, end, first_line) of every top-level function or method."""
    tree = ast.parse(source)
    out = []

    def walk(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                out.append((node.body[0].lineno, node.end_lineno, first, node.lineno))
            elif isinstance(node, ast.ClassDef):
                walk(node.body)
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                walk(getattr(node, "body", []))
                walk(getattr(node, "orelse", []))

    walk(tree.body)
    return out


def label_mask(maps: Maps, repo: str, change) -> tuple[int, str]:
    rel_old = change.old_path
    rel = change.path
    everything = maps.all_mask
    base = os.path.basename(rel)
    if base in CONFIG or rel == "tests/helpers.py" or (rel_old and os.path.basename(rel_old) in CONFIG):
        return everything, "config/helpers: all"
    if change.is_new or change.is_deleted or change.is_rename:
        if rel.endswith(".py") or (rel_old or "").endswith(".py"):
            return everything, "py file added/removed/renamed: all"
        mask = 0
        for path in {rel, rel_old} - {None}:
            mask |= maps.open_mask(path) | maps.list_mask(os.path.dirname(path) or ".")
            directory = os.path.dirname(path)
            for opened, k in maps.opens.items():
                if os.path.dirname(opened) == directory:
                    mask |= maps.mask(k)
        return mask, "data file added/removed: readers of its directory"
    if change.binary or change.mode_only:
        return maps.open_mask(rel_old) | maps.import_mask(rel_old), "binary/mode"
    if not rel.endswith(".py"):
        return maps.open_mask(rel_old) | maps.list_mask(os.path.dirname(rel_old) or "."), "data readers"
    old = open(os.path.join(repo, rel_old), encoding="utf-8").read()
    try:
        spans = function_spans(old)
    except SyntaxError:
        return everything, "old unparsable"
    removed, inserts = changed_old_lines(change)
    touched = set(removed)
    for k in inserts:
        touched |= {k, k + 1}
    funcs = set()
    for line in touched:
        owner = None
        for start, end, first, defline in spans:
            if start <= line <= end and defline < start:
                owner = (start, end, first, defline)
        if owner is None:
            importers = maps.import_mask(rel_old) | maps.open_mask(rel_old)
            if rel_old.startswith("tests/"):
                # a test module imported by nothing else still fails its own tests
                for i, node in enumerate(maps.tests):
                    if node.split("::", 1)[0] == rel_old:
                        importers |= 1 << i
            return importers, f"import-time edit at line {line}: importers"
        funcs.add(owner)
    mask = 0
    for start, end, first, defline in funcs:
        mask |= maps.entry_mask(rel_old, [first, defline]) | maps.func_mask(rel_old, range(first, end + 1))
    return mask, f"function-body edits in {len(funcs)} functions: entered/ran"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("set_dir")
    ap.add_argument("tests_json")
    ap.add_argument("repo")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--spot", type=int, default=30)
    ap.add_argument("--only", default=None)
    ap.add_argument("--timeout", type=float, default=1500.0)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--tmp", default=None)
    ap.add_argument("--all", action="store_true",
                    help="run the whole suite in one pytest process (files in tests.json order) for every patch")
    o = ap.parse_args()

    repo = os.path.realpath(o.repo)
    tests = json.load(open(o.tests_json))["tests"]
    number_of = {t["id"]: t["number"] for t in tests}
    id_of = {t["number"]: t["id"] for t in tests}
    seconds_by_id = {t["id"]: t["seconds"] for t in tests}
    by_file = {}
    for t in tests:
        by_file.setdefault(t["file"], set()).add(t["id"])
    meta = json.load(open(os.path.join(o.set_dir, "meta.json")))
    detail_path = os.path.join(o.set_dir, "label_detail.json")
    detail = json.load(open(detail_path)) if os.path.exists(detail_path) else {}
    maps = Maps()
    rng = random.Random(11)
    tmp_base = o.tmp or os.path.join(os.path.dirname(HERE), ".work", "label_tmp")
    os.makedirs(tmp_base, exist_ok=True)

    cids = sorted(meta)
    if o.only:
        cids = [c for c in o.only.split(",") if c in meta]  # keeps the given order
    work = []
    for cid in cids:
        if cid in detail and "failing" in detail[cid]:
            continue
        text = open(os.path.join(o.set_dir, "commits", cid + ".patch"), encoding="utf-8", errors="replace").read()
        changes = parse_patch(text)
        mask = 0
        reasons = []
        files = {}
        importers = 0
        for change in changes:
            m, why = label_mask(maps, repo, change)
            mask |= m
            reasons.append(f"{change.path}: {why}")
            if change.old_path:
                importers |= maps.import_mask(change.old_path)
            if change.is_deleted:
                files[change.old_path] = None
                continue
            if change.is_rename:
                files[change.old_path] = None
            old_text = None
            if change.old_path:
                with open(os.path.join(repo, change.old_path), "rb") as handle:
                    old_text = handle.read().decode("utf-8")
            files[change.path] = apply_hunks(old_text, change).encode("utf-8")
        nodes = {maps.tests[i] for i in maps.tests_of(mask)} & set(number_of)
        pool = sorted({maps.tests[i] for i in maps.tests_of(importers)} & set(number_of) - nodes)
        spot = set(rng.sample(pool, min(o.spot, len(pool))))
        if o.all:
            nodes, spot = set(number_of), set()
            reasons.append("--all: whole suite in one process")
        work.append((cid, files, sorted(nodes), sorted(spot), reasons))
    print(f"{len(work)} patches to label", file=sys.stderr)

    env = dict(os.environ)
    env.update({"SKIP_INTEGRATION": "0", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"})
    # tests run `python -c ...` subprocesses (test_lazy_load): `python` must be the
    # interpreter with the labeler's packages, as in the labeler's image
    env["PATH"] = os.path.dirname(os.path.abspath(o.python)) + os.pathsep + env.get("PATH", "")
    running = {}
    t0 = time.time()

    def start(item):
        cid, files, nodes, spot, reasons = item
        overlay = os.path.join(tmp_base, f"lab_{cid}")
        shutil.rmtree(overlay, ignore_errors=True)
        build_overlay(overlay, repo, dict(files))
        args = []
        chosen = set(nodes) | set(spot)
        missing = set()
        for f, ids in sorted(by_file.items()):
            sel = ids & chosen
            if not sel:
                continue
            if not os.path.exists(os.path.join(overlay, f)):
                # the patch deleted or moved this test file: pytest would abort the
                # whole run on the missing path; its tests count as failing
                missing |= sel
                continue
            # whole files only (the plugin deselects the rest): a single node id
            # that no longer exists would make pytest run nothing at all
            args.append(f)
        out = os.path.join(tmp_base, f"lab_{cid}.json")
        argfile = os.path.join(tmp_base, f"lab_{cid}.args")
        with open(argfile, "w") as handle:
            handle.write("\n".join(args))
        with open(out + ".missing", "w") as handle:
            json.dump(sorted(missing), handle)
        if not args:
            # nothing left to run (every chosen test's file is gone): never call
            # pytest without arguments, it would run the whole directory
            with open(out, "w") as handle:
                json.dump({"failed": [], "collect_failed": [], "ran": 0, "exitstatus": 5}, handle)
            return subprocess.Popen(["true"]), out, overlay, time.time(), open(os.devnull, "w")
        e = dict(env)
        e["BR_LABEL_OUT"] = out
        # per-test limits: generous next to each test's recorded time, so a patch
        # that makes every test hang does not cost the global limit per test
        limits_path = os.path.join(tmp_base, f"lab_{cid}.limits")
        with open(limits_path, "w") as handle:
            # generous: on a loaded machine with a 2-worker pool, test_tpcds (2.2 s
            # recorded) can take 20+ s; a 5x+5 s limit produced false failures
            json.dump({n: min(o.timeout, max(120.0, 20.0 * seconds_by_id.get(n, 1.0) + 60.0)) for n in chosen}, handle)
        e["BR_LABEL_TIMEOUTS"] = limits_path
        select_path = os.path.join(tmp_base, f"lab_{cid}.select")
        with open(select_path, "w") as handle:
            json.dump(sorted(chosen - missing), handle)
        e["BR_LABEL_SELECT"] = select_path
        e["PYTHONPATH"] = overlay + os.pathsep + HERE
        log = open(os.path.join(tmp_base, f"lab_{cid}.log"), "w")
        cmd = [o.python, "-m", "pytest", "-p", "no:cacheprovider", "-p", "br_pytest_plugin", "-q", "-o",
               "console_output_style=classic", f"--timeout={o.timeout}", "-rN", "--tb=no", f"@{argfile}"]
        proc = subprocess.Popen(cmd, cwd=overlay, env=e, stdout=log, stderr=subprocess.STDOUT)
        return proc, out, overlay, time.time(), log

    pending = list(work)
    while pending or running:
        while pending and len(running) < o.jobs:
            item = pending.pop(0)
            running[item[0]] = (item,) + start(item)
        time.sleep(1.0)
        for cid in list(running):
            item, proc, out, overlay, started, log = running[cid]
            if proc.poll() is None:
                continue
            log.close()
            del running[cid]
            _, files, nodes, spot, reasons = item
            try:
                result = json.load(open(out))
            except (OSError, ValueError):
                result = None
            if result is None:
                detail[cid] = {"error": f"pytest exit {proc.returncode}, no result", "reasons": reasons}
                print(f"{cid}: ERROR exit {proc.returncode}", file=sys.stderr)
            else:
                failed = set(result["failed"]) | set(json.load(open(out + ".missing"))) \
                    | set(result.get("not_collected", []))
                for bad in result["collect_failed"]:
                    path = bad.split("::", 1)[0]
                    failed |= {n for n in by_file.get(path, ()) if n in set(nodes) | set(spot)}
                failing = sorted(number_of[n] for n in failed if n in number_of)
                spot_fail = sorted(number_of[n] for n in failed if n in set(spot))
                detail[cid] = {"failing": failing, "ran": result["ran"], "nodes": len(nodes), "spot": len(spot),
                               "timeouts": result.get("timeouts", []),
                               "spot_failing": spot_fail, "collect_failed": result["collect_failed"],
                               "seconds": round(time.time() - started, 1), "reasons": reasons}
                flag = f" SPOT-FAIL {spot_fail}" if spot_fail else ""
                print(f"{cid} {meta[cid]['kind']}: {len(failing)} failing of {len(nodes)}+{len(spot)} "
                      f"({time.time() - started:.0f}s){flag}", file=sys.stderr)
            shutil.rmtree(overlay, ignore_errors=True)
            json.dump(detail, open(detail_path, "w"), indent=0)
    answers = {"commits": {}}
    for cid, d in sorted(detail.items()):
        if "failing" in d and cid in meta:
            answers["commits"][cid] = {"failing": d["failing"], "kind": meta[cid]["kind"]}
    json.dump(answers, open(os.path.join(o.set_dir, "answers.json"), "w"), indent=0)
    print(f"labeled {len(answers['commits'])} in {time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
