"""Write maps/enums.json: names of sqlglot Enum classes whose == is identity-like.

Used by the selector to treat `x is E.MEMBER` and `x == E.MEMBER` as the same.
Enums mixed with int are excluded (an int compares equal to them).

Run from a sqlglot checkout at the pinned commit:
  python tools/build_enums.py <out.json>
"""

import enum
import importlib
import inspect
import json
import pkgutil
import sys

import sqlglot


def is_identity_enum(klass: type) -> bool:
    return issubclass(klass, enum.Enum) and not issubclass(klass, int)


def main() -> None:
    names: set[str] = set()
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name == "sqlglot.__main__":
            continue
        try:
            module = importlib.import_module(info.name)
        except Exception:  # noqa: BLE001 - optional modules may be missing
            continue
        for _, klass in inspect.getmembers(module, inspect.isclass):
            if is_identity_enum(klass):
                names.add(klass.__name__)
    with open(sys.argv[1], "w", encoding="utf-8") as handle:
        json.dump(sorted(names), handle, indent=0)
    print(f"{len(names)} enums", file=sys.stderr)


if __name__ == "__main__":
    main()
