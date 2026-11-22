"""Module attribute reads per test (build-time / offline tool).

A zygote imports every sqlglot module and swaps each module object's class for a
subclass of ModuleType whose __getattribute__ records the attribute name. Each test
then runs alone in a child and records which attributes of which sqlglot modules it
read through the module object (`exp.UNWRAPPED_QUERIES`, `getattr(module, name)`,
`from M import NAME` executed at runtime). Reads of a module's own globals by its own
code (bare names) and of names bound by `from M import NAME` at import time do not go
through the module object; the selector keeps its static rules for those. Import-time
reads happen in the zygote and are not recorded either (they only matter through the
state they build, which the static rules and keyrefine's fingerprints cover).

Usage: python tools/trace_modattrs.py <sqlglot checkout> <tests.json> <out.json> [--jobs N] [--limit S]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import trace_tests  # noqa: E402

LOG: set = set()
RECORDING = [False]


class RecModule(types.ModuleType):
    def __getattribute__(self, name):
        if RECORDING[0] and not (name.startswith("__") and name.endswith("__")):
            LOG.add((id(self), name))
        return types.ModuleType.__getattribute__(self, name)


def swap_all() -> dict:
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
    names = {}
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "sqlglot" or name.startswith("sqlglot.")):
            continue
        if type(module) is types.ModuleType:
            module.__class__ = RecModule
            names[id(module)] = name
    return names


def child(node, names, out_path, limit):
    from selector.runner import run_tests

    LOG.clear()
    RECORDING[0] = True
    failed, _ = run_tests([node], limit_of=lambda n: limit)
    RECORDING[0] = False
    reads = sorted({f"{names[mid]}\t{attr}" for mid, attr in LOG if mid in names})
    with open(out_path, "w") as handle:
        json.dump({"failed": node in failed, "reads": reads}, handle)


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
    names = swap_all()
    print(f"{len(names)} recording modules", file=sys.stderr)
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
                    child(t["id"], names, part + ".tmp", o.limit)
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
            print(f"module trace failed for {t['id']} (status {status})", file=sys.stderr)
        if done % 200 == 0:
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
        merged[node] = data["reads"]
    missing = sorted(set(number_to_id.values()) - set(merged) - set(dropped))
    json.dump({"reads": merged, "dropped": sorted(dropped), "missing": missing}, open(o.out, "w"))
    print(f"module-read records for {len(merged)} tests; dropped {len(dropped)}; missing {len(missing)}", file=sys.stderr)


if __name__ == "__main__":
    main()
