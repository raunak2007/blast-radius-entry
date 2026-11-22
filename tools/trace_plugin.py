"""pytest plugin: record which non-Python files under the repo each test opens.

Collection-time opens (no test running yet) are attributed to the test file whose
frame is on the stack, as FILE:<path>. Output: JSON {key: [relative paths]}.
Enable with:  TRACE_ROOT=<repo> TRACE_OUT=<file> pytest -p trace_plugin
"""
import json
import os
import sys

ROOT = os.path.realpath(os.environ["TRACE_ROOT"])
OUT = os.environ["TRACE_OUT"]
_current = [None]
_records = {}


def _hook(event, args):
    if event == "subprocess.Popen":
        key = _current[0]
        if key is not None:
            _records.setdefault("<subprocess>", set()).add(key)
        return
    if event != "open":
        return
    path = args[0]
    if isinstance(path, int):
        return
    if isinstance(path, bytes):
        path = path.decode("utf-8", "replace")
    try:
        full = os.path.realpath(os.path.join(os.getcwd(), str(path)))
    except Exception:
        return
    if not full.startswith(ROOT + os.sep) or full.endswith(".pyc") or "__pycache__" in full:
        return
    key = _current[0]
    if full.endswith(".py") and key is None:
        return  # imports during collection; only source reads inside tests count
    rel = os.path.relpath(full, ROOT)
    if key is None:
        frame = sys._getframe(1)
        while frame is not None:
            name = frame.f_code.co_filename
            if name.startswith(os.path.join(ROOT, "tests") + os.sep) and name.endswith(".py"):
                key = "FILE:" + os.path.relpath(name, ROOT)
                break
            frame = frame.f_back
        if key is None:
            key = "UNKNOWN"
    _records.setdefault(key, set()).add(rel)


sys.addaudithook(_hook)


def _class_key(nodeid):
    parts = nodeid.split("::")
    return "CLASS:" + "::".join(parts[:2]) if len(parts) >= 3 else "FILE:" + parts[0]


def pytest_runtest_setup(item):
    # setUp / setUpClass / setUpModule opens belong to every test of the class
    _current[0] = _class_key(item.nodeid)


def pytest_runtest_call(item):
    _current[0] = item.nodeid


def pytest_runtest_teardown(item, nextitem):
    _current[0] = _class_key(item.nodeid)


def pytest_runtest_logfinish(nodeid, location):
    _current[0] = None


def pytest_sessionfinish(session, exitstatus):
    out = OUT
    if os.environ.get("PYTEST_XDIST_WORKER"):
        out = f"{OUT}.{os.environ['PYTEST_XDIST_WORKER']}"
    with open(out, "w") as fh:
        json.dump({k: sorted(v) for k, v in _records.items()}, fh)
