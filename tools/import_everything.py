"""Import every sqlglot module and build every dialect, so coverage can record which
lines run at import time (metaclasses, class-creation factories, module tables).

Run: coverage run --source=sqlglot tools/import_everything.py   (cwd = sqlglot checkout)
"""

import importlib
import pkgutil
import sys

import sqlglot
from sqlglot.dialects.dialect import Dialect, Dialects


def import_all_modules() -> None:
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name == "sqlglot.__main__":
            continue
        try:
            importlib.import_module(info.name)
        except Exception as error:  # noqa: BLE001 - optional modules may be missing
            print(f"skip {info.name}: {error}", file=sys.stderr)


def build_all_dialects() -> None:
    for dialect in Dialects:
        if not dialect.value:
            continue
        instance = Dialect.get_or_raise(dialect.value)
        for attr in ("tokenizer_class", "parser_class", "generator_class", "tokenizer", "jsonpath_tokenizer"):
            try:
                getattr(instance, attr)
            except Exception as error:  # noqa: BLE001
                print(f"{dialect.value}.{attr}: {error}", file=sys.stderr)
        try:
            instance.parser()
            instance.generator()
        except Exception as error:  # noqa: BLE001
            print(f"{dialect.value}: {error}", file=sys.stderr)


if __name__ == "__main__":
    import_all_modules()
    build_all_dialects()
