"""Which lambdas in sqlglot/ are called before any test runs.

An edit confined to a lambda's body changes nothing until the lambda is called, so it reaches
exactly the tests whose coverage shows the lambda's line running (the line itself also runs at
import, where the lambda is created, but that is outside every test's coverage). That only holds
for a lambda nothing calls while modules are imported: a lambda called then (by a builder in a
class body, say) shapes import-time state.

This starts a fresh interpreter with a profile hook, imports every sqlglot module and every test
module, instantiates every dialect (as the map's usage plugin does before the tests), then does
test_execute_tpcds's table setup (tables built from tpcds.pkl.gz, pickled as the test's pool
does), and records the (file, first line) of every lambda code object in sqlglot/ that was
called. Lambdas called in the table setup are refused too, because the TPC-DS replay reuses
that setup from the pristine sources.

usage: python build_import_lambdas.py <sqlglot repo> <tpcds.pkl.gz> <out.pkl.gz>
"""
import gzip
import importlib
import os
import pickle
import pkgutil
import sys

repo, tpcds_path, out_path = sys.argv[1:4]
repo = os.path.realpath(repo)
tpcds_path, out_path = os.path.realpath(tpcds_path), os.path.realpath(out_path)
sys.path.insert(0, repo)
os.chdir(repo)
os.environ.setdefault("SKIP_INTEGRATION", "0")
prefix = os.path.join(repo, "sqlglot") + os.sep
called = set()


def hook(frame, event, arg):
    if event == "call":
        code = frame.f_code
        if code.co_name == "<lambda>":
            f = os.path.realpath(code.co_filename)
            if f.startswith(prefix):
                called.add((os.path.relpath(f, repo), code.co_firstlineno))


sys.setprofile(hook)
import sqlglot  # noqa: E402

names = sorted(m.name for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot.") if m.name != "sqlglot.__main__")
tests = []
for dirpath, _, files in os.walk(os.path.join(repo, "tests")):
    for f in files:
        if f.startswith("test_") and f.endswith(".py"):
            tests.append(os.path.relpath(os.path.join(dirpath, f), repo)[:-3].replace(os.sep, "."))
failed = []
for name in names + sorted(tests):
    try:
        importlib.import_module(name)
    except BaseException as e:  # noqa: BLE001
        failed.append((name, repr(e)[:120]))
from sqlglot.dialects.dialect import Dialect, Dialects  # noqa: E402

for d in Dialects:
    try:
        Dialect.get_or_raise(d.value)
    except Exception as e:  # noqa: BLE001
        failed.append((d.value, repr(e)[:120]))
from sqlglot.executor.table import Table  # noqa: E402

old = pickle.load(gzip.open(tpcds_path))
tables = {t: Table(columns=c, rows=old["tables"][t]) for t, c in old["schema"].items()}
pickle.loads(pickle.dumps((old["schema"], tables)))
sys.setprofile(None)

out = {}
for rel, line in called:
    out.setdefault(rel, set()).add(line)
with gzip.open(out_path, "wb") as fh:
    pickle.dump({"called": {k: sorted(v) for k, v in sorted(out.items())}, "failed": failed}, fh)
print(f"{len(called)} lambdas called before tests, in {len(out)} files; {len(failed)} imports failed: {failed[:5]}")
