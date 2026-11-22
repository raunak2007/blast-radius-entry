"""Key-level records of string-keyed dicts (build-time / offline tool).

See selector/keyrec.py. A zygote imports every sqlglot module, then replaces every
exact `dict` with only string keys held by a sqlglot class (in its own namespace) or
module by a recording copy (`RecDict`; one copy per original object, so aliases such
as `Presto.TIME_MAPPING is MySQL.TIME_MAPPING` stay aliases). Any original dict that
is still referenced from somewhere else afterwards (a default argument, a closure, a
container) is listed as "unrecordable": its lookups through that other reference would
not be seen, so the selector never narrows by its records. Each test then runs alone in
a child forked from the zygote and records which keys of which dict it looked up and
which dicts it enumerated ("*"). Import-time reads of sqlglot modules happen in the
zygote and are not recorded: the selector learns what an edit changes at import time by
fingerprinting the patched state (keyrec.state_fingerprint).

Usage: python tools/trace_keys.py <sqlglot checkout> <tests.json> <out.json> [--jobs N] [--limit S]
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import trace_tests  # noqa: E402

from selector import keyrec  # noqa: E402


def wrap_all():
    """Import all sqlglot modules and swap their string-keyed dicts for RecDicts.
    Returns (holders: id(RecDict) -> [holder\\tattr], unrecordable: [holder\\tattr])."""
    import importlib
    import pkgutil

    import sqlglot

    for item in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if item.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(item.name)
        except Exception as error:  # noqa: BLE001
            print(f"cannot import {item.name}: {error!r}", file=sys.stderr)
    replacement = {}  # id(original) -> RecDict
    originals = {}  # id(original) -> original (kept alive for the referrer check)
    holders: dict[int, list] = {}
    seen = set()

    def swap(owner_obj, holder, attr, value):
        if not keyrec.string_keyed(value):
            return
        rec = replacement.get(id(value))
        if rec is None:
            rec = keyrec.RecDict(value)
            replacement[id(value)] = rec
            originals[id(value)] = value
        setattr(owner_obj, attr, rec)
        holders.setdefault(id(rec), []).append(f"{holder}\t{attr}")

    def visit_class(cls):
        if id(cls) in seen:
            return
        seen.add(id(cls))
        key = f"{cls.__module__}:{cls.__qualname__}"
        for attr, value in list(vars(cls).items()):
            if isinstance(value, type) and value.__module__ == cls.__module__ \
                    and value.__qualname__.startswith(cls.__qualname__ + "."):
                visit_class(value)
            elif type(value) is dict:
                try:
                    swap(cls, key, attr, value)
                except (TypeError, AttributeError):
                    pass  # read-only class namespace: leave it (never narrowed: no records)
            elif isinstance(value, keyrec.RecDict):
                holders.setdefault(id(value), []).append(f"{key}\t{attr}")

    for name, module in sorted(sys.modules.items()):
        if module is None or not (name == "sqlglot" or name.startswith("sqlglot.")):
            continue
        for attr, value in list(vars(module).items()):
            if attr.startswith("__") and attr.endswith("__"):
                continue
            if isinstance(value, type) and value.__module__ == name:
                visit_class(value)
            elif type(value) is dict:
                swap(module, name, attr, value)
            elif isinstance(value, keyrec.RecDict):
                holders.setdefault(id(value), []).append(f"{name}\t{attr}")
    # Originals still referenced elsewhere: their lookups through that reference would
    # be missed, so the selector must not narrow by these records.
    unrecordable = []
    gc.collect()
    ignore = {id(originals), id(replacement)}
    for oid in list(originals):
        original = originals[oid]
        referrers = [r for r in gc.get_referrers(original)
                     if id(r) not in ignore and type(r).__name__ != "frame"]
        if referrers:
            rec = replacement[oid]
            unrecordable.extend(holders.get(id(rec), []))
            if os.environ.get("KEYS_DEBUG") and len(unrecordable) < 40:
                for r in referrers[:3]:
                    desc = type(r).__name__
                    if isinstance(r, dict):
                        desc += f" keys={[k for k, v in r.items() if v is original][:3]} name={r.get('__name__')}"
                    print("referrer", holders.get(id(rec), [])[:1], desc, file=sys.stderr)
    originals.clear()
    return holders, sorted(set(unrecordable))


def child(node, holders, out_path, limit):
    from selector.runner import run_tests

    keyrec.LOG.clear()
    keyrec.RECORDING[0] = True
    failed, _ = run_tests([node], limit_of=lambda n: limit)
    keyrec.RECORDING[0] = False
    lookups: dict[str, set] = {}
    for rid, key in keyrec.LOG:
        for holder in holders.get(rid, ()):
            lookups.setdefault(holder, set()).add(key)
    with open(out_path, "w") as handle:
        json.dump({"failed": node in failed, "lookups": {h: sorted(k) for h, k in lookups.items()}}, handle)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("tests_json")
    ap.add_argument("out")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--limit", type=float, default=600.0)
    o = ap.parse_args()
    root = os.path.realpath(o.repo)
    o.out = os.path.abspath(o.out)
    parts = o.out + ".parts"
    os.makedirs(parts, exist_ok=True)
    tests = json.load(open(o.tests_json))["tests"]
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    trace_tests.preload()
    keyrec.RECORDING[0] = False
    holders, unrecordable = wrap_all()
    print(f"{len(holders)} recording dicts, {len(unrecordable)} unrecordable holders", file=sys.stderr)
    json.dump({"unrecordable": unrecordable, "holders": sorted({h for hs in holders.values() for h in hs})},
              open(o.out + ".meta", "w"))
    pending = [t for t in sorted(tests, key=lambda t: -t["seconds"])
               if not os.path.exists(os.path.join(parts, f"{t['number']}.json"))]
    running = {}
    done = 0
    while pending or running:
        while pending and len(running) < o.jobs:
            t = pending.pop(0)
            part = os.path.join(parts, f"{t['number']}.json")
            pid = os.fork()
            if pid == 0:
                code = 0
                try:
                    devnull = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(devnull, 1)
                    os.dup2(devnull, 2)
                    child(t["id"], holders, part + ".tmp", o.limit)
                    os.rename(part + ".tmp", part)
                except BaseException:  # noqa: BLE001
                    code = 1
                finally:
                    os._exit(code)
            running[pid] = t
        pid, status = os.wait()
        t = running.pop(pid, None)
        done += 1
        if t and status != 0:
            print(f"key trace failed for {t['id']} (status {status})", file=sys.stderr)
        if done % 100 == 0:
            print(f"{done} done, {len(pending)} pending", file=sys.stderr)
    merged, dropped = {}, []
    number_to_id = {str(t["number"]): t["id"] for t in tests}
    for name in os.listdir(parts):
        if not name.endswith(".json"):
            continue
        data = json.load(open(os.path.join(parts, name)))
        node = number_to_id[name[:-5]]
        if data["failed"]:
            dropped.append(node)
            continue
        merged[node] = data["lookups"]
    missing = sorted(set(number_to_id.values()) - set(merged) - set(dropped))
    json.dump({"lookups": merged, "unrecordable": unrecordable, "dropped": sorted(dropped), "missing": missing},
              open(o.out, "w"))
    print(f"key records for {len(merged)} tests; dropped {len(dropped)}; missing {len(missing)}", file=sys.stderr)


if __name__ == "__main__":
    main()
