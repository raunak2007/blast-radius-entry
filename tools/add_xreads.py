"""Add class-name attribute reads to maps/classinfo.json.gz (build-time tool).

Every attribute access whose receiver is a class spelled by name (`MySQL.TIME_MAPPING`,
`Hive.Tokenizer.KEYWORDS`, `TimeUnit.VAR_LIKE`) in sqlglot/, sqlglotc/ or tests/, resolved
against the live modules of the pinned checkout: `__xreads__ = [[file, line, class key, attribute]]`.
A class attribute read this way needs no instance of the class, so the selector counts
the tests that ran such a line as users of the class (and of its subclasses' readers).
Enum classes are left out (their members are read everywhere; edits of an enum body are
never narrowed by class usage).

Usage: python tools/add_xreads.py <sqlglot checkout> <classinfo.json.gz in> <out>
"""

import ast
import enum
import gzip
import importlib
import json
import os
import pkgutil
import sys


def main():
    repo, src, out = os.path.realpath(sys.argv[1]), sys.argv[2], sys.argv[3]
    info = json.load(gzip.open(src, "rt"))
    os.chdir(repo)
    sys.path.insert(0, repo)
    os.environ["SKIP_INTEGRATION"] = "0"
    import sqlglot

    modules = {}
    for item in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        try:
            modules[item.name] = importlib.import_module(item.name)
        except Exception as error:  # noqa: BLE001
            print(f"cannot import {item.name}: {error!r}", file=sys.stderr)
    for base in ("tests",):
        for dirpath, dirnames, filenames in os.walk(os.path.join(repo, base)):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "fixtures")]
            for name in filenames:
                if name.endswith(".py"):
                    mod = os.path.relpath(os.path.join(dirpath, name), repo)[:-3].replace("/", ".")
                    try:
                        modules[mod] = importlib.import_module(mod)
                    except Exception as error:  # noqa: BLE001
                        print(f"cannot import {mod}: {error!r}", file=sys.stderr)
    # classes by simple name, for receivers bound by a function-local import
    by_name = {}
    for mod in list(modules.values()):
        for value in list(vars(mod).values()):
            if isinstance(value, type) and (getattr(value, "__module__", "") or "").startswith("sqlglot"):
                by_name.setdefault(value.__name__, set()).add(value)
    rows = set()
    unresolved = 0
    for name, mod in modules.items():
        path = getattr(mod, "__file__", None) or ""
        if not path.endswith(".py"):
            continue
        rel = os.path.relpath(path, repo)
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            parts = []
            value = node.value
            while isinstance(value, ast.Attribute):
                parts.append(value.attr)
                value = value.value
            if not isinstance(value, ast.Name):
                continue
            parts.append(value.id)
            parts.reverse()
            starts = [mod] if hasattr(mod, parts[0]) else []
            if not starts and parts[0] in by_name:
                # bound by a function-local import: every sqlglot class of that name
                starts = [type("_", (), {parts[0]: c}) for c in by_name[parts[0]]]
            resolved = []
            for start in starts:
                obj = start
                for part in parts:
                    obj = getattr(obj, part, None) if obj is not None else None
                if obj is not None:
                    resolved.append(obj)
            if not resolved:
                if parts[0][:1].isupper():
                    unresolved += 1  # class-body locals (KEYWORDS.pop), classes defined inside tests, ...
                    if os.environ.get("XREADS_DEBUG"):
                        print("unresolved", rel, node.lineno, ".".join(parts) + "." + node.attr, file=sys.stderr)
                continue
            for obj in resolved:
                if not isinstance(obj, type) or issubclass(obj, enum.Enum):
                    continue
                module_name = getattr(obj, "__module__", "") or ""
                if not module_name.startswith("sqlglot"):
                    continue
                key = f"{module_name}:{obj.__qualname__}"
                rows.add((rel, node.end_lineno or node.lineno, key, node.attr))
                if node.lineno != (node.end_lineno or node.lineno):
                    rows.add((rel, node.lineno, key, node.attr))
    info["__xreads__"] = sorted(rows)
    with gzip.open(out, "wt", compresslevel=9) as handle:
        json.dump(info, handle, separators=(",", ":"))
    print(f"{len(rows)} class-name reads; {unresolved} capitalised receivers not resolved", file=sys.stderr)


if __name__ == "__main__":
    main()
