# Blast Radius entry

Picks the sqlglot tests each patch can break, then packs them onto workers.

**Sample set (30 commits):** 97.5 s simulated, 0 misses, about 7 s of container CPU (the best possible on this set is 94.4 s). The starter scores 1210.5 s.

## How it decides

Every map comes from running sqlglot's own tests at the pinned commit (`eb11aec4`), which the track rules allow. No patch or answer file is used to build anything.

- **Comment-only hunks** (comments or blank lines outside strings) change nothing Python runs, so they select nothing.
- **A changed statement inside a function** selects the tests that executed it (per-test line coverage).
- **A changed class attribute, or a new method** selects the tests that used that class: the dialects it serves (and their subclasses), or the expression type and its subclasses.
- **A changed fixture or data file** selects the tests that opened it.
- **A changed test file** selects the edited tests (or their class or file).
- **Anything risky** runs every test: a patch that won't apply, a syntax error, a module-level change other modules import, config files, renames and deletions.

- **Cheap selections are checked by running them.** When the selected tests cost less to run than to schedule (each worker costs 2 s), they run on the patched sources in a forked child (patched imports, process pools forked and capped at 2 workers) and the ones that pass are dropped. Anything that crashes, times out or can't run faithfully stays selected.
- **Class-attribute edits are narrowed** to the tests that read the attribute, ran the edited lambda, or built the `exp.X` keyed by the edited dict entry.

- **Comparison edits are checked against recorded operands.** A second run of the suite at the pinned commit records, per test, the operands every single-operator comparison in `sqlglot/` saw (`tools/operand_plugin.py`). When an edit only swaps a comparison's operator or changes a literal operand (`is None` → `== None`, `<` → `<=`, `3` → `4`), the old and new comparison are evaluated on those operands, and a test whose every result is unchanged is dropped. Unknown types, custom `__eq__`, comparisons run at import, operands we couldn't record and tests that ran the line without a recorded comparison all keep the test. The idea comes from the skbgp entry; the implementation is ours.
- **Dict entry edits select the tests that read that key.** The same run wraps every module- and class-level dict literal and records which keys each test reads, and which tests read the dict in bulk (`items()`, `copy()`, `==`, ...). A one-line change to one entry's literal value selects only those tests. A dict that import-time code read in bulk, or whose key import-time code read (as `{**Base.X}` does), keeps the usual selection.

- **The 21 s TPC-DS test is replayed instead of run** when every change is ordinary function code in `sqlglot/` that the test's table setup doesn't run. Only the queries whose recorded coverage reaches a changed line are replayed. If the change stays inside `optimize()`, the optimizer output is compared. Otherwise the full execution result is compared. The duckdb transpilation is compared in both cases. Any difference, error or timeout keeps the test, and the replay only happens when its estimated CPU is well under the time it saves.

Tests are packed longest-first, trying 1 to 8 workers and keeping the cheapest.

## Files

| File | What it is |
| --- | --- |
| `selector.py` | The program the scorer runs (stdlib only) |
| `blast_map.pkl.gz` | Coverage, file reads, and dialect/expression usage per test |
| `operands.pkl.gz` | Per-test comparison operands and dict keys read (`tools/operand_plugin.py`, `tools/build_operands.py`) |
| `tpcds.pkl.gz` | Per-query coverage, CPU and pristine outputs of `test_execute_tpcds`, plus its tables (`tools/build_tpcds.py`) |
| `sqlglot_src.tar.gz` | The pinned commit's Python sources, which patches are applied to |
| `tools/` | Rebuilds the data files from the pinned commit (`tools/build_map.sh`). `mutate_compares.py`, `mutate_dicts.py` and `check_dropped.py` check the two refinements on synthetic mutants |
| `Makefile`, `score.py`, `requirements.lock` | From the starter kit, unchanged |

The build needs no network beyond the base image.

## Test locally

Copy the kit's `sample/` folder in (it's git-ignored), then:

```sh
make sample
```
