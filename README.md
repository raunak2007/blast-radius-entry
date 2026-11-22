# blast-radius

Coverage-guided test selection for sqlglot patches, built for the RWX "Blast Radius" track of the Builders Buildathon 2026.

**Status:** working prototype, under active development during the hackathon.

## Overview

Each input is a patch against sqlglot at commit `eb11aec4`. The selector prints, per patch, which tests to run and how to split them across up to 8 workers. Every missed failing test costs 300 s, and every selected test costs its runtime, so the goal is to pick the smallest set that is still safe.

## How it works

1. **Maps built once, offline, from the pinned commit** (`tools/`):
   - `maps/coverage.json.gz`: for every source line, the tests that executed it (pytest-cov with per-test contexts). Process pools in the test suite are run inline, with pickling preserved, so their work is attributed to the right test.
   - `maps/fixtures.json`: which tests open which files under `tests/fixtures/`.
2. **Per patch, at run time** (`br/`):
   - Each hunk is applied on its own and both versions are parsed. If the syntax tree is unchanged (comments, blank lines), the hunk is ignored.
   - A changed statement inside a function or lambda selects the tests that executed any line of that statement.
   - Import-time changes (class or module level, new methods, signatures) first fall back to the tests that touched that class, module or dialect family.
   - The import check (below) then compares every class and module attribute after the patched import with the unpatched import. When only plain data changed, the selection becomes the tests that read a changed attribute at run time, within that class's users. A constant passed into a constructor keyword and stored as `self.<kw>` is followed to the code that reads the stored attribute.
   - Fixture edits select the tests that read the fixture. SQL comment-only fixture edits select nothing.
   - Syntax errors, deleted modules and unknown files select every test. Any internal error falls back to every test.
3. **Running tests while deciding** (allowed by the rules, CPU is charged):
   - A selection of at most 2 recorded seconds is run on the patched code in a fresh import (process pools inline, subprocesses refused), and only the tests that fail are kept. A change no test catches then costs 0 instead of 2 s or more.
   - `test_execute_tpcds` (21.5 s) is decided by running only the TPC-DS queries whose per-query coverage (`maps/tpcds_queries.json.gz`) touches a function-body change.
4. **Scheduling** (`br/schedule.py`): longest-first packing, trying 1 to 8 workers and keeping the cheapest (busiest worker + 2 s per worker).

## Layout

| Path | Purpose |
| --- | --- |
| `selector.py` | Entry point run by the image |
| `br/diff.py` | Unified diff parsing and hunk application |
| `br/impact.py` | Patch to affected tests |
| `br/schedule.py` | Worker split |
| `br/importcheck.py` | Forked import check: crashes, import-time state diff, running small selections |
| `br/statefp.py` | Content fingerprints of class and module attributes |
| `br/readers.py` | Statements that read a class or module name |
| `br/probe.py` | TPC-DS query probe for `test_execute_tpcds` |
| `maps/` | Coverage and fixture maps from the pinned commit |
| `tools/` | Scripts that rebuild the maps |
| `sample/`, `score.py`, `SCORING.md` | Organiser's sample set and scorer |

## Usage

```sh
make sample      # needs Docker: build, run on the 30 sample commits, score
```

Without Docker, against a local sqlglot checkout:

```sh
BR_DATA=sample BR_SQLGLOT=path/to/sqlglot python selector.py > output.jsonl
python score.py sample sample/answers.json output.jsonl
```

Rebuilding the maps (sqlglot checkout at `eb11aec4`, `requirements.lock` plus `pytest-cov` installed):

```sh
cd path/to/sqlglot
INLINE_POOLS=1 SKIP_INTEGRATION=0 FIXTURE_TRACE_OUT=../trace/fx PYTHONPATH=.:../../tools \
  python -m pytest -p fixture_tracer -n 8 --cov=sqlglot --cov=tests --cov-context=test --cov-report=
python tools/build_maps.py path/to/sqlglot sample/tests.json ../trace maps && gzip -9 -f maps/coverage.json
```

## Results

Local `score.py` on the organiser's 30 sample commits (simulated seconds, CPU not included):

| Version | Simulated seconds | Misses |
| --- | --- | --- |
| Starter (run everything on 3 workers) | 1210.5 | 0 |
| Oracle (exactly the failing tests, best packing) | 94.4 | 0 |
| v1 line coverage + AST no-op detection | 178.3 | 0 |
| v3 + class-setup attribution, class-usage tracing | 177.0 | 0 |
| v4 + `is`/`==` equivalence for None and identity enums | 126.8 | 0 |
| v5–v7 + constant readers, import-crash check, import-time groups | 118.4 | 0 |
| v8.1 + import-state diff, TPC-DS probe, running cheap selections | 107.5 | 0 |

Practice board (hidden commits, middle of 3 runs):

| Version | Score | Simulated | CPU | Missed commits |
| --- | --- | --- | --- | --- |
| v1 `c46568b` | 497.0 s | 495.6 s | 1.4 s | 1 |
| v4 `b085d47` | 196.9 s | 195.0 s | 1.9 s | 0 |
| v6 `298a323` | 192.7 s | 184.8 s | 7.9 s | 0 |
| v7 `9831f14` | 187.3 s | 184.8 s | 2.5 s | 0 |
| v8 `7e77201` | 184.8 s | 171.2 s | 13.7 s | 0 |
| v8.1 `7a278a8` (probe and test-run costs priced at board CPU speed) | 179.9 s | 171.6 s | 8.3 s | 0 |

Measuring against the oracle: `tools/waste.py` runs the selected tests of each saved mutant to get its true failing set, and `tools/rescore.py` re-scores the current selector on those (simulated seconds, CPU not included):

| Version | 104 mutants (mostly class constants) | Misses |
| --- | --- | --- |
| Oracle | 117.7 | 0 |
| v7 | 662.0 | 0 |
| v8 | about 497 | 0 |

Mutation testing (`tools/mutate.py`: random operator, constant, boolean, comment, fixture and class-constant edits, with every unselected test run for real): about 450 mutants so far (120 in a row on v8.1 with no miss). The blind spots it found are all fixed: a dialect class constant (v3), an import-time crash in a subclass (v5), a metaclass/trie edit that runs at import (v6), and in v8 dataclass field defaults, enum member values, and classes in lazily imported modules missing from the class-usage map.

## Limitations

- Coverage is line-level. Class-level edits rely on class-usage tracing (which tests constructed the class or a subclass); a class whose attributes are read without ever being instantiated falls back to module or dialect-family coverage.
- The `is`/`==` equivalence assumes no object with an unusual `__eq__` reaches the changed comparison; only the executor's sort wrapper is known to have one, and its file is excluded.
- Maps were built on macOS arm64 with Python 3.13. The scorer runs linux/amd64, where a few platform-dependent paths might differ.
- Running tests while deciding assumes they behave the same in one process as under pytest workers. Process pools run inline (with pickling), which differs from a spawned worker only in process-global state.
- The state diff sees state reachable from sqlglot's module globals. Effects stored elsewhere (other libraries, objects only reachable through caches) are invisible to it.
- Hot-path flags (read on almost every parse) still select most of their dialect's tests, because line coverage cannot see which branch a test took.

## License

MIT, see [LICENSE](LICENSE).
