"""Which lambdas run while sqlglot is imported? Prints IMPORT_RUN_LAMBDAS for selector.py.

usage: python tools/build_import_lambdas.py <sqlglot checkout at the pinned commit>

Imports every sqlglot module and every test module (the way collection does), then builds an
executor table as the TPC-DS replay's setup does, with a sys.monitoring hook on function
starts, and lists the lambdas in sqlglot/ whose body ran: {file: first lines}. The DuckDB generator and Hive's type annotations parse SQL templates when
they are imported, which runs entries of the parser's lookup tables; an edit inside such a
lambda can change what those imports build, so the selector never narrows it to the tests
that ran the lambda. Reads nothing but the pinned sources.
"""
import collections
import glob
import importlib
import os
import pkgutil
import sys

root = os.path.realpath(sys.argv[1])
sys.path.insert(0, root)
os.environ["SKIP_INTEGRATION"] = "0"
os.chdir(root)
seen = collections.defaultdict(set)
mon = sys.monitoring
TOOL = mon.DEBUGGER_ID
mon.use_tool_id(TOOL, "import-lambdas")
prefix = os.path.join(root, "sqlglot") + os.sep


def start(code, offset):
    if code.co_name != "<lambda>" or not code.co_filename.startswith(prefix):
        return mon.DISABLE
    seen[os.path.relpath(code.co_filename, root)].add(code.co_firstlineno)


mon.register_callback(TOOL, mon.events.PY_START, start)
mon.set_events(TOOL, mon.events.PY_START)
import sqlglot  # noqa: E402

failed = []
names = sorted(m.name for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot.") if not m.name.endswith("__main__"))
names += sorted(p[:-3].replace(os.sep, ".") for p in glob.glob("tests/**/test_*.py", recursive=True))
for name in names:
    try:
        importlib.import_module(name)
    except BaseException as error:  # noqa: BLE001
        failed.append(f"{name}: {type(error).__name__}: {str(error)[:80]}")
# what the TPC-DS replay data counts as that test's setup, besides the imports: building the
# executor's tables and sending them through pickle (tools/build_tpcds_ctx.py)
import pickle  # noqa: E402

from sqlglot.executor.table import Table, ensure_tables  # noqa: E402

built = {"t": Table(columns=("a", "b"), rows=[(1, "x"), (2, None)])}
pickle.loads(pickle.dumps(({"t": {"a": "INT", "b": "TEXT"}}, built)))
ensure_tables(built, dialect=None)
mon.set_events(TOOL, 0)
for line in failed:
    print("# import failed:", line, file=sys.stderr)
print("IMPORT_RUN_LAMBDAS = {")
for rel in sorted(seen):
    print(f"    {rel!r}: frozenset(({', '.join(str(n) for n in sorted(seen[rel]))},)),")
print("}")
