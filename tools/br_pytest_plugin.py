"""pytest plugin for tools/label_pytest.py: record which node ids failed (dev tool).

A test fails when any of its reports fails: setup/call/teardown errors, a
failing subtest (pytest 9 reports SUBFAILED next to PASSED), a timeout
(pytest-timeout raises inside the test), an unexpected success. A file that
cannot be collected fails every test of that file.
"""

import json
import os

# Keep sqlglot's spawn process pools (sized by the CPU count) small on a shared
# machine; this changes how many workers run the cases, not what they compute.
_CPUS = int(os.environ.get("BR_LABEL_CPUS", "2"))
os.cpu_count = lambda: _CPUS
if hasattr(os, "process_cpu_count"):
    os.process_cpu_count = lambda: _CPUS

FAILED = set()
COLLECT_FAILED = set()
RAN = set()


SELECTED = None
COLLECTED = set()


def pytest_collection_modifyitems(session, config, items):
    """Keep only the node ids listed in BR_LABEL_SELECT (files are passed whole,
    since one node id that no longer exists makes pytest run nothing), and apply
    per-test timeouts from BR_LABEL_TIMEOUTS: a patch that makes every test hang
    must not cost the global limit per test."""
    global SELECTED
    import pytest

    select_path = os.environ.get("BR_LABEL_SELECT")
    if select_path:
        SELECTED = set(json.load(open(select_path)))
        kept = [item for item in items if item.nodeid in SELECTED]
        dropped = [item for item in items if item.nodeid not in SELECTED]
        if dropped:
            config.hook.pytest_deselected(items=dropped)
        items[:] = kept
    COLLECTED.update(item.nodeid for item in items)
    path = os.environ.get("BR_LABEL_TIMEOUTS")
    if path:
        limits = json.load(open(path))
        for item in items:
            if item.nodeid in limits:
                item.add_marker(pytest.mark.timeout(limits[item.nodeid]))


TIMEOUTS = set()


def pytest_runtest_logreport(report):
    RAN.add(report.nodeid)
    if report.failed:
        FAILED.add(report.nodeid)
        if "Timeout" in str(getattr(report, "longrepr", "") or ""):
            TIMEOUTS.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        COLLECT_FAILED.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("BR_LABEL_OUT")
    if out:
        with open(out, "w") as handle:
            json.dump({"failed": sorted(FAILED), "collect_failed": sorted(COLLECT_FAILED),
                       "not_collected": sorted((SELECTED or set()) - COLLECTED),
                       "timeouts": sorted(TIMEOUTS),
                       "ran": len(RAN), "exitstatus": int(exitstatus)}, handle)
