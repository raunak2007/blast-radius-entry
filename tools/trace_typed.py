"""Typed coverage of class-attribute reads (build-time / offline tool).

For every line of sqlglot that reads a class-level constant through `self.NAME`
or `cls.NAME` (NAME in UPPER_CASE, the convention for dialect settings and
tables), record, per test, every class `self`/`cls` had when the line ran. A
change to `NAME` in the body of class C can only be seen by such a line when the
receiver is C or a subclass of C.

Like tools/trace_tests.py, each test runs alone in a child forked from a zygote
that never imported sqlglot or the tests, so cold-start work (imports, lazily
built caches) is part of every test that needs it. Records of a test that fails
or times out here are dropped, and the selector only narrows with records: a
test that ran a line (coverage map) without a typed record for it is kept.

Usage: python tools/trace_typed.py <sqlglot checkout> <tests.json> <out.json> [--jobs N] [--skip id,id]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import trace_tests  # noqa: E402

ATTR = re.compile(r"\b(?:self|cls)\.([A-Z][A-Z0-9_]+)\b")
# Dialect settings read through the parser/generator/tokenizer's dialect object.
DIALECT = re.compile(r"\bself\.dialect\.([A-Z][A-Z0-9_]+)\b")


def interesting_lines(root):
    out = {}
    for dirpath, dirnames, names in os.walk(os.path.join(root, "sqlglot")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in names:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            lines = {}
            for i, line in enumerate(open(path, encoding="utf-8").read().split("\n"), start=1):
                if line.lstrip().startswith("#"):
                    continue
                kinds = (1 if ATTR.search(line) else 0) | (2 if DIALECT.search(line) else 0)
                if kinds:
                    lines[i] = kinds
            if lines:
                out[path] = lines
    return out


def child(root, node, interesting, out_path, limit):
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    mon.use_tool_id(tool, "blast-radius-typed")
    import inspect

    record: dict = {}
    record_dialect: dict = {}
    mro: dict = {}  # receiver class key -> keys of its runtime MRO
    root_slash = root + os.sep
    getframe = sys._getframe

    def note(cls):
        key = f"{cls.__module__}:{cls.__qualname__}"
        bases = {f"{c.__module__}:{c.__qualname__}" for c in getattr(cls, "__mro__", (cls,))}
        mro.setdefault(key, set()).update(bases)
        return key

    def on_line(code, line):
        lines = interesting.get(code.co_filename)
        kinds = lines.get(line) if lines is not None else None
        if not kinds or code.co_argcount == 0:
            return mon.DISABLE
        first = code.co_varnames[0]
        if first not in ("self", "cls"):
            return mon.DISABLE
        try:
            obj = getframe(1).f_locals.get(first)
        except Exception:  # noqa: BLE001
            obj = None
        rel = code.co_filename[len(root_slash):]
        if kinds & 1:
            cls = obj if (first == "cls" and isinstance(obj, type)) else type(obj)
            record.setdefault(rel, {}).setdefault(line, set()).add(note(cls))
        if kinds & 2 and first == "self":
            # self.dialect without running descriptors: an instance attribute or a slot
            try:
                raw = inspect.getattr_static(obj, "dialect", None)
                if type(raw).__name__ == "member_descriptor":
                    raw = getattr(obj, "dialect", None)
                elif isinstance(raw, (property, staticmethod, classmethod)) or callable(raw):
                    raw = None
            except Exception:  # noqa: BLE001
                raw = None
            if raw is not None:
                record_dialect.setdefault(rel, {}).setdefault(line, set()).add(note(type(raw)))
        return None

    mon.register_callback(tool, mon.events.LINE, on_line)
    from selector.runner import run_tests

    mon.set_events(tool, mon.events.LINE)
    try:
        failed, _ = run_tests([node], limit_of=lambda n: limit)
    finally:
        mon.set_events(tool, 0)
    data = {"failed": node in failed, "mro": {k: sorted(v) for k, v in mro.items()},
            "typed": {f: {str(l): sorted(k) for l, k in lines.items()} for f, lines in record.items()},
            "typed_dialect": {f: {str(l): sorted(k) for l, k in lines.items()} for f, lines in record_dialect.items()}}
    with open(out_path, "w") as handle:
        json.dump(data, handle)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("tests_json")
    ap.add_argument("out")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--skip", default="")
    ap.add_argument("--limit", type=float, default=120.0)
    o = ap.parse_args()
    root = os.path.realpath(o.repo)
    o.out = os.path.abspath(o.out)
    parts = o.out + ".parts"
    os.makedirs(parts, exist_ok=True)
    tests = json.load(open(o.tests_json))["tests"]
    skip = set(filter(None, o.skip.split(",")))
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    trace_tests.preload()
    interesting = interesting_lines(root)
    print(f"{sum(len(v) for v in interesting.values())} interesting lines in {len(interesting)} files", file=sys.stderr)
    pending = [t for t in sorted(tests, key=lambda t: -t["seconds"]) if t["id"] not in skip
               and not os.path.exists(os.path.join(parts, f"{t['number']}.json"))]
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
                    child(root, t["id"], interesting, part + ".tmp", o.limit)
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
            print(f"typed trace failed for {t['id']} (status {status})", file=sys.stderr)
        if done % 100 == 0:
            print(f"{done} done, {len(pending)} pending", file=sys.stderr)
    merged = {}
    merged_dialect = {}
    mro = {}
    dropped = []
    number_to_id = {str(t["number"]): t["id"] for t in tests}
    for name in os.listdir(parts):
        if not name.endswith(".json"):
            continue
        data = json.load(open(os.path.join(parts, name)))
        node = number_to_id[name[:-5]]
        if data["failed"]:
            dropped.append(node)
            continue
        merged[node] = data["typed"]
        merged_dialect[node] = data.get("typed_dialect", {})
        for k, v in data.get("mro", {}).items():
            mro.setdefault(k, set()).update(v)
    json.dump({"typed": merged, "typed_dialect": merged_dialect, "mro": {k: sorted(v) for k, v in mro.items()},
               "dropped": sorted(dropped)},
              open(o.out, "w"))
    print(f"typed records for {len(merged)} tests; dropped {dropped}", file=sys.stderr)


if __name__ == "__main__":
    main()
