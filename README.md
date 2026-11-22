# Blast Radius entry

Picks the sqlglot tests each patch can break, then packs them onto workers.

**Sample set (30 commits):** 94.4 s simulated, 0 misses, about 2.9 s of CPU. The starter scores 1210.5 s.

## How it decides

Every map comes from running sqlglot's own tests at the pinned commit (`eb11aec4`), which the track rules allow. No patch or answer file is used to build anything.

- **Comment-only hunks** (comments or blank lines outside strings) change nothing Python runs, so they select nothing.
- **A changed statement inside a function** selects the tests that executed it (per-test line coverage). For a statement spread over several lines, the function is compiled before and after the edit and only the lines holding changed instructions count (a jump whose target moved counts where it stands), so a short-circuited condition or an untaken branch on its own line doesn't pull in every test that ran the statement.
- **A changed class attribute, or a new method** selects the tests that used that class: the dialects it serves (and their subclasses), or the expression type and its subclasses.
- **A changed fixture or data file** selects the tests that opened it.
- **A changed test file** selects the edited tests (or their class or file).
- **Anything risky** runs every test: a patch that won't apply, a syntax error, a module-level change other modules import, config files, renames and deletions.

- **Cheap selections are checked by running them.** When the selected tests cost less to run than to schedule (each worker costs 2 s), they run on the patched sources in a forked child (patched imports, process pools forked and capped at 2 workers) and the ones that pass are dropped. Anything that crashes, times out or can't run faithfully stays selected. The parent freezes its heap before forking (`gc.freeze()`), so a check's collector never walks, or copies the pages of, the maps and imports it inherited, and checks collect young objects less often. The parent also imports sqlglot's core once (the 37 files a bare `import sqlglot` loads). A check whose patch changes none of them keeps that copy and imports the rest, the patched module included, as before; any other check forgets every sqlglot module and imports all of it again.
- **What a check costs is estimated per test.** `test_optimizer`'s heavy tests (most run fixtures through a process pool, and they are the ones an edit most often breaks) are charged 3.7 times their measured check CPU, so they are rarely worth running; the file's other tests cost what they take. `TestExecutor`'s `setUpClass` loads the TPC-H and TPC-DS tables (1.1 s). A check skips it when the commit changes nothing but ordinary function code outside `sqlglot/executor/table.py`, the one file `setUpClass` runs code from, and none of the tests being checked touches what it builds (told from the test file's source). Then `setUpClass` does exactly what it did before, so it can't fail, and those tests run the same without it.

- **Fixture-driven tests are checked on the entries an edit can reach.** `test_optimize`, `test_simplify`, `test_annotate_funcs` and 26 other tests loop over the entries of a `.sql` fixture, and they cover so much code that most edits select one of them. `tools/build_fixture_cov.py` records, per entry, the lines its processing runs: in an ordinary run of the test, and alone in a fresh process, so code that only runs the first time something is used counts for every entry that needs it. When a commit changes nothing but ordinary function code, and at most a fifth of a test's entries can run a changed line, the check runs the real test with its fixture loader yielding only those entries. Every other entry never runs changed code, so it does what it did at the pinned commit, where the test passes. On the sample this drops `test_optimize` after 1 of its 94 entries.

- **Class-attribute edits are narrowed** to the tests that read the attribute, ran the edited lambda, or built the `exp.X` keyed by the edited dict entry. For the parser's function tables, an entry is narrowed to the tests that looked up its old or new name unless an import-time parse looks that name up or something writes it at runtime (`tools/build_key_level.py`). Tokenizer `KEYWORDS` entries likewise, unless the key is popped or checked at import time, looked up by a tokenizer while modules import, or has a space or single-token character (the keyword trie) (`tools/build_keywords_level.py`).

- **The 21 s TPC-DS test is replayed instead of run** when every change is ordinary function code in `sqlglot/` that the test's table setup doesn't run. Only the queries whose recorded coverage reaches a changed line are replayed, each checked by what the line can affect for that query. If it only runs in parsing, `optimize()` or the duckdb transpilation, the optimizer output is compared. If it also runs in the debug-log rendering `execute()` does next (whose text is dropped), that rendering must not raise or change the expression. If it runs in planning or execution, the full result is compared. The duckdb transpilation is always compared. An optimizer output that differs only in node metadata counts as unchanged for queries whose rendering, planning and execution never read metadata (all 57, checked by `tools/build_tpcds_meta.py`). Any difference, error or timeout keeps the test, and the replay only happens when its estimated CPU is well under the time it saves. The replay also stops once it has used 3 s plus six times the recorded CPU of the queries it has started, so an edit that makes a plan explode fails fast instead of using the whole budget. The tables' rows stay pickled, per table, until a query runs to the end (`tools/pack_tpcds.py`): a replay that only compares optimizer output never loads them.

- **Recorded operands** (`tools/operand_plugin.py`, a second profiled run of the suite at the pinned commit). Every single-operator comparison in `sqlglot/` is compiled to record the operands it sees per test, and every module- or class-level dict literal is wrapped when it is created, so its reads are recorded per key even through a reference another object keeps (the tokenizer holds on to `NUMERIC_ESCAPES`), and reads made while modules import are recorded so those keys are refused. An edit that only swaps a comparison's operator or changes a literal operand is re-evaluated on those operands, and tests whose recorded results can't change are dropped. A one-line edit to one dict entry's plain value, where our own per-name key index gives up, selects the tests that read that key or read the dict in bulk. Both files are packed per source file so a run unpickles only what its commits touch.

- **Import-time effects are judged per instruction.** A statement in a function that also runs at import (a builder called in a class body) only widens the selection to everything importing the module when a changed instruction itself ran at import. The body of a lambda or closure such a builder returns never runs then, so an edit there selects the tests that ran it. Every line a fresh process runs while importing all of sqlglot and its tests is in the map's import-time lines, which is what this relies on.

- **A changed def line** (a default value, say) selects the tests that ran the function, and, when its body also runs while modules are imported, everything an import-time change selects: a default the import-time template parses take differently can stop a module importing at all. Overload stubs (`...` bodies) are exempt.

- **Import-time state is checked, not assumed changed.** When a changed function line also ran while modules were imported (the parser runs while the DuckDB generator parses its SQL templates), the selection is normally widened to everything importing them. If the narrower selection would save at least 3 s over what the wide one costs after its own run check (a wide selection that is cheap to run is just run), a fresh interpreter (fixed hash seed, CPU and memory capped) imports every sqlglot module and the six test modules whose import runs sqlglot code, once with the patch and once without, and fingerprints all module and class state: values, containers, expression trees, function defaults and closures, the objects they reach, and which imports fail. Code is left out, since what runs later is what coverage decides; tuples are compared by value and class-only sequences by name, since their identity and order follow memory addresses. Equal fingerprints mean the edit only matters where its lines run in tests, and the commit is planned (TPC-DS replay included) as ordinary function code.

- **Decorators, base classes and lambdas in class or module code** go through the same check instead of selecting every user of the class or module. A lambda's body is code, which the fingerprint leaves out, so an edit inside one selects the tests that ran the lambda. Plain value edits (a constant, a call's arguments) skip the check, since they almost always change what the import builds. Each function's fingerprint includes its def line in the unpatched numbering, so it matters which of two same-named defs got bound, but lines a patch inserts above a def don't.

- **`test_lazy_load`** is the one test that starts a second interpreter, and all that interpreter does is `import sqlglot`. So it is only selected when a patch changes one of the files that import loads, or something that isn't plainly another sqlglot module or a file under `tests/`.

- **A changed class constant** (an UPPER_CASE class attribute's literal value, for names no import-time code reads, or, when some does, after the import-time state check finds nothing changed but that constant) selects the tests whose reads of it resolved to that class: `tools/attrs_plugin.py` replaces every such constant with a descriptor that returns the same value and records the reader, so a read through a subclass that doesn't override it counts, and one through a subclass that does doesn't. `ClickHouseGenerator.TABLE_HINTS` goes from the 736 tests that read any `TABLE_HINTS` to none.

- **A changed default value** selects only the tests in which some call left that parameter out (`tools/defaults_plugin.py`, sys.monitoring on every module- and class-level function with defaults; an explicit argument that is the very same object counts as left out).

- **Checks run on commits that also change a data file** (a fixture next to a code edit), on the tests that never open the changed file; its readers stay selected. The TPC-DS replay, the generator memory check, the executor checks without `setUpClass` and the fixture-entry checks apply to such commits too, for tests that never open the changed file: on 150 mutants with a code edit, a fixture edit and a comment this cut 143 s, most of it from 12 TPC-DS replays.

- **A TPC-DS replay child may allocate at most 1.25 GB beyond what it inherits**, so an edit that makes one query's intermediate results explode fails its own replay instead of exhausting the run's 4 GB. (Test checks are left uncapped: duckdb reserves far more address space than it uses.)

- **Code that runs while other modules import** (the trie builder every tokenizer class calls, the error formatter a dialect's import-time parse reaches) selects every test that uses those modules, a module whose import imports them, or imports one of them while it runs (`from sqlglot.dialects import X` inside a test) (`import_users.pkl.gz`), and the generator's first-use dispatch table selects every test that builds a generator.

Tests are packed longest-first, trying 1 to 8 workers and keeping the cheapest.

## Files

| File | What it is |
| --- | --- |
| `selector.py` | The program the scorer runs (stdlib only) |
| `blast_map.pkl.gz` | Coverage, file reads, and dialect/expression usage per test |
| `tpcds.pkl.gz` | Per-query coverage, CPU and pristine outputs of `test_execute_tpcds`, plus its tables (`tools/build_tpcds.py`, then `tools/build_tpcds_ctx.py`, which records each query's coverage from a fresh pool worker, split by what checks each part) |
| `fixtures.pkl.gz` | Per fixture-driven test and fixture entry, the lines its processing runs (`tools/build_fixture_cov.py`, `tools/pack_fixtures.py`) |
| `attr_owners.pkl.gz` | Per class constant, the tests whose reads resolved to it (`tools/attrs_plugin.py`, `tools/build_attr_owners.py`) |
| `defaults.pkl.gz` | Per function and defaulted parameter, the tests in which a call left it at its default (`tools/defaults_plugin.py`, `tools/build_defaults.py`) |
| `operands.pkl.gz`, `dictreads.pkl.gz` | Per test, the operands each comparison saw and the keys each module/class-level dict literal gave out (`tools/operand_plugin.py`, `tools/build_operands.py`, repacked by `tools/build_lazy.py`) |
| `import_users.pkl.gz` | Function-body lines that run while other modules are imported, with the modules that affects (`tools/build_import_users.py`) |
| `sqlglot_src.tar.gz` | The pinned commit's Python sources, which patches are applied to |
| `tools/` | Rebuilds both data files from the pinned commit (`tools/build_map.sh`) |
| `Makefile`, `score.py`, `requirements.lock` | From the starter kit, unchanged |

The build needs no network beyond the base image.

## Test locally

Copy the kit's `sample/` folder in (it's git-ignored), then:

```sh
make sample
```
