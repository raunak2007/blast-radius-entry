
# Candidate submissions (branch br-opt)

> **Final entry if the cutoff were now: M19 (e73afb6)** — published (public d96e97c),
> fresh-pull checked, practice 165.2 s (140.205 simulated, same as M17) with 0 missed.
> The equal simulated time does not show that its state comparisons narrowed on the
> scorer: on official-style sets they barely change the simulated cost (see "CPU
> diagnosis" at the end).
>
> **Recommended next: M22 (52f588a)** — M21 plus a gate that runs the state
> comparison only when planning with its likely result saves more than its scorer CPU
> (val9/val6: 0 comparisons instead of 11/8, same simulated seconds). Every validation
> set 0 misses; Docker-checked on its exact tree (`docker build --no-cache` 195 s;
> valid, 0 misses, 96.839 s emulated, done at 93 s; M21 needed 126 s and got 121.616
> because its comparisons ate the emulated time budget). Fallback option: M21
> (55d8c1b), also Docker-checked. Known robustness issue: in the local emulated
> container (not natively, and evidently not on the scorer) the state comparisons fall
> back because tuples/dicts built from sets of classes (address-hashed) are ordered
> differently in the build-time pinned run and the run-time patched run, and the
> build's two seed runs agreed, so they were not marked seed-dependent. Fallback is
> sound (wider masks) but loses the narrowing; a fix is being tested.

Newest last. Numbers are local: simulated seconds from the kit's `score.py` (exact for
that output), oracle = packing exactly the failing tests, misses must be 0. "CPU" is
user+sys of the whole local run on a loaded Apple M1 (native, not emulated); the
scorer's CPU for the same work is unknown (the first submission used 6.45 s on the
~40 practice commits), so read it as relative.

Reference (first submission, 01c632d on the public repo): practice 195.8 s
(189.352 simulated + 6.45-6.49 CPU, 0 missed).

| set | commits | oracle |
| --- | ---: | ---: |
| kit sample | 30 | 94.4 |
| val1 | 126 | 208.6 |
| val2 | 68 | 169.7 |
| val4 | 36 | 12.4 |

## M1 — per-case restricted runs of fixture-loop tests + planner cost model

Commit: **7c3bd18** (branch br-opt). Docker re-run on exactly this tree: valid, 0 misses.

What changed:
* TPC-DS/TPC-H execution and optimizer tests (and every other test that loops over
  fixture cases) can be verified on only the cases a patch can affect: cases whose
  fixture text changed, or whose own per-case coverage (traced at build time as
  pseudo-tests in the coverage map) touches the changed code.
* Planner: per-test failure probabilities (measured), a test counts as too expensive to
  run only above max(3 s, 60% of its recorded seconds), and "verify the best
  seconds-per-CPU tests, include the cheap tail" options.

| set | before sim | after sim | misses | CPU before -> after |
| --- | ---: | ---: | ---: | ---: |
| kit sample | 109.5 | 106.3 | 0 | 16.7 -> 18.5 |
| val1 | 344.5 | 330.0 | 0 | 56.4 -> 56.4 |
| val2 | 352.2 | 277.8 | 0 | 30.7 -> 62.2 |
| val4 | 205.9 | 106.6 | 0 | 62.4 -> 113.7 |

Docker, scorer flags, linux/amd64 emulated on the M1, kit sample: valid output, 0
misses, finished in ~133 s wall; 3 verifications were killed by their CPU budget under
emulation (as before: emulation runs 5-10x slower), so the emulated simulated score is
higher than native (157.6).

Expected on practice: simulated down where a TPC-DS/TPC-H test is a candidate but does
not fail (each such commit was 25.45 s; now ~5-12 s of CPU to clear it); CPU up by a
few seconds. Risk: restricted runs rely on fixture cases being independent (checked by
reading every listed loop) and on per-case coverage; any reason a test is a candidate
that does not reach one of its traced cases forces a full run.

Real result of M1 (from SUBMISSIONS.md): 185.3 s = 171.045 simulated + 14.1-14.5 CPU,
0 missed (first entry 189.352 + 6.5). Simulated -18.3 s, CPU +7.8 s.

## M2 — typed coverage for class attributes + no-op statements

Commit: **4df374d** (branch br-opt). Builds on M1. Docker re-run on exactly this tree.

What changed:
* A changed class attribute read only as `self.NAME` / `cls.NAME` (dialect settings
  such as `JOIN_HINTS`, `LIMIT_ONLY_LITERALS`) reaches only tests that ran that line
  with a receiver of the changed class or a subclass (typed coverage: runtime class
  and MRO of `self`/`cls` per test, from a fresh interpreter; no record = kept).
* Inside function bodies, adding/removing/swapping `pass`, bare constants, or stores
  of constants to names the function never mentions is a no-op.
* Same planner and CPU scale as M1.

| set | M1 sim | M2 sim | misses | M2 local CPU |
| --- | ---: | ---: | ---: | ---: |
| kit sample (30) | 106.3 | 106.3 | 0 | 17.0 |
| val1, original 126 | 330.0 | 321.9 | 0 | (in the 141 run) |
| val1 now 141 (+15 expensive, pytest labels) | n/a | 690.4 (oracle 364.5) | 0 | 115.9 |
| val2 (68) | 277.8 | 276.6 | 0 | 59.8 |
| val4 (36) | 106.6 | 106.6 | 0 | 101.9 |
| val5 new kinds (15 labelled so far) | 109.7 | 49.6 (oracle 40.5) | 0 | 3.1 |

Docker, scorer flags, emulated amd64, kit sample: valid, 0 misses, 106 s wall.

Expected on practice: small (the sample is unchanged; gains are on class-level flag
edits and dead-code/docstring edits). Low risk: both changes only narrow where a
record proves the receiver class or the statement is provably inert.

Calibration note: the planner's per-job CPU estimate is ~0.79x the measured local CPU
(measured/estimate = 1.26 on val4+val5). Telling the planner CPU is 0.6x as expensive
changed nothing on the kit sample except one wasted verification (j6uz: both candidates
fail anyway) and on val1 cost +84 s local CPU for -21.9 s simulated, so the scale stays.
The remaining practice-like excess (val2, uniform file choice like the official
generator) is dominated by TPC-DS execution included when an optimizer edit is run by
most TPC-DS cases (9scn, g7wf, gzdj: 68 of val2's 107 s); that is the next target.

## M3 — TPC-DS execution verified by comparing optimizer outputs

Commit: **4cf2213** (branch br-opt). Builds on M2. Docker re-run on exactly this tree.

What changed: when every reason `test_execute_tpcds` is a candidate only reaches code
its cases run inside `optimize()` (coverage outside `optimize()` traced per case at
build time), the verifier runs the affected cases only up to the end of `optimize()`
and compares fingerprints of the call's input, output and input-after-call with the
ones recorded at the pinned commit (recorded under two hash seeds and once with all
cases in one process: identical for all 99 cases). Equal: the test cannot fail.
Different: run those cases in full when that is cheap, otherwise include the test.
Typical cost: ~0.2 s warm-up + ~0.1 s per case (map units), e.g. 5.6 s for all 57
executed cases, instead of ~36 s to execute them.

| set | M2 sim | M3 sim | misses | M3 local CPU (M2) |
| --- | ---: | ---: | ---: | ---: |
| kit sample (30) | 106.3 | 106.3 | 0 | 27.0 (17.0) |
| val1 (141) | 690.4 | 690.4 | 0 | 127.3 (115.9) |
| val2 (68) | 276.6 | **242.1** | 0 | 83.5 (59.8) |
| val4 (36) | 106.6 | **87.0** | 0 | 88.9 (101.9) |
| val5 (46 labelled) | 151.1 | 151.1 | 0 | 10.0 |
| TPC-DS check set (28 commits from all sets) | 509.8 | 455.7 | 0 | |

Docker, scorer flags, emulated amd64, kit sample: valid, 0 misses, finished within the
deadline (132 s wall emulated; 3 verifications killed by emulation slowness as before).

Expected on practice: helps whenever an optimizer edit reaches most TPC-DS cases and
does not break execution (val2's 9scn and g7wf: 25.5 s -> 9.1 s and 7.4 s). Cost: one
stage check (~4 s on the scorer at the observed CPU ratio) when such an edit does
break TPC-DS (the sample's 2jrq: +8.6 s local CPU, simulated unchanged).
Risk: relies on code outside optimize() behaving identically when optimize() returns
an identical tree (it is deterministic code fed identical inputs); residual gap as for
all coverage-based selection: state shared across cases inside one process.

CPU scale: also tried 0.7 with M3 on val2: -7.7 s simulated, +14.1 s local CPU, i.e.
break-even at a 0.5 scorer/local CPU ratio, so the scale stays at 1.0.

## M4 — re-run only the TPC-DS cases whose optimizer output changed (submitted: 165.0)

Commit: **0fdd4d8** (branch br-opt). Builds on M3. Docker re-run on exactly this tree
(valid, 0 misses, 131 s wall emulated).

What changed: a stage check matches `optimize()` calls to cases by the fingerprint of
their input; the cases whose output differs (or whose call is missing) are run in full
when that costs less than including the test (e.g. val2 gzdj: 4 of 32 cases differ ->
re-run 4 -> TPC-DS cleared: 25.45 s -> 10.0 s).

| set | M3 sim | M4 sim | misses | M4 local CPU |
| --- | ---: | ---: | ---: | ---: |
| kit sample (30) | 106.3 | 106.3 | 0 | 27.3 |
| val1 (141) | 690.4 | 690.4 | 0 | 122.4 |
| val2 (68) | 242.1 | **226.7** | 0 | 90.3 |
| val4 (36) | 87.0 | 87.0 | 0 | 92.0 |
| val5 (now all 64 labelled) | n/a | 209.7 (oracle 128.9) | 0 | 13.8 |
| TPC-DS check set (28) | 455.7 | 440.3 | 0 | |

Versus M1 (practice 185.3): on val2, the closest proxy for the official generator,
simulated 277.8 -> 226.7 (-51 s / 68 commits); sample unchanged; local CPU on val2
62 -> 90 s (about +14 s at the observed ~0.5 scorer/local ratio).

Real result of M4 (SUBMISSIONS.md): 165.0 s = 143.412 simulated + 21.4-21.7 CPU, 0 missed.
Scorer/local CPU ratio: practice 21.6 s / 40 commits = 0.54 s per commit vs 0.91 s per
commit locally on the kit sample (27.3 s / 30) -> about 0.6 (0.4 against val2).

## M5 — import checks (soundness), re-export-aware reads, dialect-typed coverage, stage abort (superseded by M6)

Commit: **7a966cc** (branch br-opt). Builds on M4. Docker re-run on exactly this tree
(valid, 0 misses).

What changed:
* Soundness: an import-time edit (class body, module level, decorators, defaults)
  whose new value is not provably safe to evaluate now always gets its module
  imported by the verifier, also when the plan is blind or nothing is reachable (a
  zero-test job); if the import fails or cannot be checked, every importer is
  included. Before, such an edit (e.g. a class-body value that raises at import) was
  narrowed by class usage and, on a blind plan, never checked. Checked with a
  synthetic tsql class-body edit that raises: 171 tests fail (pytest), 171 selected.
* A module-level value change that keeps the same names bound: `from M import NAME`
  lines are no longer reads (their later uses are; re-exports followed transitively);
  computed-name readers that only use classes are skipped for plain-data values
  (val6 7bef, a TIMEZONES entry: 1617 -> 93 candidates).
* Typed coverage also records the class of `self.dialect` on `self.dialect.NAME`
  lines (dialect settings read by the shared parser/generator).
* A TPC-DS stage check stops as soon as the test will be included anyway (sample
  2jrq: 8.6 s -> ~5 s local CPU).

| set | M4 sim | M5 sim | misses | local CPU M4 -> M5 |
| --- | ---: | ---: | ---: | ---: |
| kit sample (30) | 106.3 | 106.3 | 0 | 27.3 -> 23.8 |
| val1 (141) | 690.4 | 690.4 | 0 | 122.4 -> 125.5 |
| val2 (68) | 226.7 | 226.7 | 0 | 90.3 -> 88.5 |
| val4 (36) | 87.0 | 87.0 | 0 | 92.0 -> 87.9 |
| val3 (40 class-level edits, pytest labels) | 367.6 | **350.0** | 0 | 57.9 -> 42.8 |
| val5 (64 new kinds) | 209.7 | 209.7 | 0 | 13.8 -> 13.8 |
| val6 (41 official-style mutations, pytest labels) | 156.7 | **145.3** | 0 | 48.9 -> 36.5 |

One reported miss during this round was a false label: val6 bmwq listed
test_max_nodes as failing because the labeller's per-test limit (149 s) expired under
load; run alone on the patched tree it passes (45 s). Label corrected, limits widened.

Expected on practice: CPU slightly down; simulated down only where module-level data,
dialect settings or class-level values are edited. Main value: the import-check
soundness fix for unseen kinds of change in the final.

## M6 — function-scoped computed-name reads, typed records for the slowest tests (submitted: 157.2)

Commit: **d1cc829** (branch br-opt). Builds on M5 (so it carries M5's import-check
soundness fix). Docker re-run on exactly this tree (valid, 0 misses).

What changed on top of M5:
* A computed-name read (`getattr`/`vars`/`__dict__`) inside a function or lambda
  only counts for tests that ran that function; the module body executing the same
  line just defines it. The executor's `EXTRACT` lambda (`getattr(e, this)`) shares its
  line with a module-level dict, so before, every module- or class-level edit
  reachable from the executor also selected TPC-DS execution.
* Typed coverage now also has records for test_execute_tpcds and test_max_nodes
  (they were skipped before, which kept them as candidates for dialect-setting edits).
* One cached AST index per file for name/import/attribute scans: candidate sets
  identical to M5 on all 420 local commits, ~30% less analysis CPU.

| set | M5 sim | M6 sim | misses | M6 local CPU (M5) |
| --- | ---: | ---: | ---: | ---: |
| kit sample (30) | 106.3 | 106.3 | 0 | 20.5 (23.8) |
| val1, M5's 141 | 690.4 | **667.3** | 0 | |
| val1 now 199 (+58 pytest labels) | n/a | 1120.8 (oracle 717.9) | 0 | 175.9 |
| val2 (68) | 226.7 | 226.7 | 0 | 70.9 (88.5) |
| val4 (36) | 87.0 | 87.0 | 0 | 78.8 (87.9) |
| val3 (40 class-level) | 350.0 | **292.0** | 0 | 45.0 (42.8) |
| val5 (64 new kinds) | 209.7 | 209.7 | 0 | 10.2 (13.8) |
| val6, M5's 41 official-style | 145.3 | **96.4** | 0 | |
| val6 now 57 | n/a | 186.9 (oracle 159.6) | 0 | 36.8 |

(Local CPU varies with machine load; compare within a row only roughly.)

Expected on practice: simulated down for module-level data and dialect/class-setting
edits that TPC-DS used to be dragged into (val6 7bef and 9q8d: 25.5 s each before),
CPU slightly down. Risk: the function-scope rule relies on line coverage separating
function code from module code (the tracer records them separately) and on locating
the getattr/vars/__dict__ call on that line (a line where none is found keeps the old
rule).

## M7 — fallbacks for unreadable patches, fast planner packing (superseded by M8)

Commit: **32e3c98** (branch br-opt). Builds on M6. Docker re-run on exactly this tree
(`git archive` of the commit): valid, 0 misses (emulated amd64; 3 verifications killed
by emulation slowness, as for every candidate).

What changed:
* Robustness: text that parses to no diff section, or a patch with two sections for one
  file, now runs everything (before: "nothing reachable", 0 tests); a section with no
  hunks and no mode change counts as an unseen (binary) change; analysis stops reasoning
  after half the time budget. Checked on 17 malformed/edge patches natively and in Docker.
* Planner option estimates pack big selections without the quadratic move/swap pass
  (memoised). Selections identical on every set; planning CPU on val1 28.2 s -> 1.0 s,
  val3 15.9 -> 0.3 s (only commits with ~1600-test blind plans were affected; sample
  0.08 s either way).
* val2 +52 and val6 +3 commits labelled with pytest; val4's 36 and 25 of val2's 68 older
  in-process labels re-checked with pytest: identical.

## M8 — per-case runs for the other 23 fixture-loop tests of test_optimizer (superseded by M9)

Commit: **8b8c280** (branch br-opt). Builds on M7. Docker re-run on exactly this tree
(`git archive` of the commit): valid, 0 misses, done at 126 s of the emulated run (2
verifications killed by emulation slowness, as before). Map 668 KB -> 893 KB.

What changed: per-case coverage and CPU for test_optimize, test_qualify_columns,
test_annotate_funcs, test_simplify, test_merge_subqueries and the 18 other
`check_file` loops of tests/test_optimizer.py (4,064 cases). Each case was traced in a
child of a zygote that had imported the tests, and its record is unioned with the
test's cold zero-case record (imports + the code outside the loops), so it covers at
least what a cold run of that case covers. Verification of such a test runs only the
cases that reach the change, under the same rule as M1 (every reason that reaches the
test must reach a kept run, else the whole test runs).

| set | M6/M7 sim | M8 sim | misses | M8 extra verify CPU (planner estimate, local) |
| --- | ---: | ---: | ---: | ---: |
| kit sample (30) | 106.3 | **104.8** | 0 | +1.3 |
| val1 (199) | 1120.8 | **1105.5** | 0 | +17.3 |
| val2 (120) | 609.3 | **581.4** | 0 | +19.4 |
| val4 (36) | 87.0 | **79.3** | 0 | +1.2 |
| val3 (40 class-level) | 292.0 | **276.9** | 0 | +8.5 |
| val5 (64 new kinds) | 209.7 | **203.5** | 0 | +2.5 |
| val6 (60 official-style) | 197.6 | **193.2** | 0 | +0.1 |

Measured local CPU is not comparable this hour (other jobs on the machine pushed the
load average to ~40; commits with unchanged plans took ~2x the CPU of the earlier run),
so the CPU column is the planner's own estimate. At the observed scorer/local CPU
ratio (~0.5) every set gains net. Expected on practice: ~1.5 s simulated less on a
sample-like batch for ~0.7 s more CPU; more where optimizer code is edited.
Risk (not verified): the per-case risk of M1 now applies to 23 more tests: a case is
skipped when its own run (alone) does not reach the change, which assumes no earlier case
of the same test leaves state behind that makes it reach the change in a full run. Each
case parses its SQL afresh; whether shared objects such as a test's schema are mutated
across cases has not been checked. Consistency check done: for all 28 per-case tests,
every line the full run covers (at the pinned commit) is covered by the zero-case run or
by some case run alone (0 lines only in the full run).

## M9 — tokenizer settings narrowed through the tokenizer core (superseded by M10; includes M7, M8)

Commit: **f3572a4** (branch br-opt). Builds on M8. Docker re-run on exactly this tree
(`git archive` of the commit): valid, 0 misses, done at 126 s of the emulated run (1
verification killed at the emulated deadline).

What changed:
* `Tokenizer._init_core` copies the tokenizer settings into a `TokenizerCore`
  (`numeric_escapes=self.NUMERIC_ESCAPES`, ...). That line runs whenever a tokenizer
  is built, so an edit of any setting in a dialect's Tokenizer selected every test
  that tokenized that dialect (sample 4d63: 358 tests for a Snowflake
  `NUMERIC_ESCAPES` entry). New build-time record (`tools/trace_tokcore.py`, each test
  alone in a fresh interpreter, 1620/1620 recorded): which lines of
  `tokenizer_core.py` that observe a setting field ran for cores built by which
  Tokenizer class. The `_init_core` line now counts only for tests that ran an
  observing line of that field for the changed class or a subclass
  (`selector/tokcore.py`). Off if the patch touches the core or `_init_core`, or
  mentions `TokenizerCore`/`_init_core` elsewhere.
* A line that uses a changed class attribute only for its truth value (following a
  local copied from it) is dropped when both values have the same truth; for a dict
  display whose keys stay the same, `key in X` lines are dropped too (the Dialect
  metaclass only tests `"x" in NUMERIC_ESCAPES`). `NAME: annotation` statements
  without a value are not reads.
* Candidates: 4d63 358 -> 7, val2 muzi 316 -> 20, vfxf -> 4, w47z -> 12 (3 failing,
  all kept), icb8 -> 1, val3 7upf -> 7.

| set | M8 sim | M9 sim | misses | verify CPU estimate M8 -> M9 (local) |
| --- | ---: | ---: | ---: | ---: |
| kit sample (30) | 104.8 | **96.8** (oracle 94.4) | 0 | 23.8 -> 22.6 |
| val1 (199) | 1105.5 | 1105.5 | 0 | 136.0 -> 136.0 |
| val2 (120) | 581.4 | **552.8** | 0 | 157.7 -> 155.3 |
| val4 (36) | 79.3 | 79.3 | 0 | 42.8 -> 42.8 |
| val3 (40 class-level) | 276.9 | **270.6** | 0 | 31.4 -> 30.7 |
| val5 (64 new kinds) | 203.5 | 203.5 | 0 | 9.4 -> 9.4 |
| val6 (60 official-style) | 193.2 | 193.2 | 0 | 49.8 -> 49.8 |
| val7 = 44 generated tokenizer-setting edits (pytest labels, 15 with failures) | 562.8 | **70.8** (oracle 32.8) | 0 | |
| val8 = 24 hand-picked tokenizer-setting edits: set elements, dict keys removed/added, strings, token types (pytest labels, 13 with failures) | 402.6 | **214.9** | 0 | |

Expected on practice: one sample commit in 30 was such an edit (4d63: 9.5 s in M7,
7.9 s in M8, 0 s in M9: its 7 remaining candidates are verified in 1.1 s local CPU);
M9's sample total is 2.5 s above the oracle.
Risk: the record is per test at the pinned commit; it assumes a test's path through
the core up to the first read of the changed field does not depend on that field
(true by construction: identical inputs and code until the field is read) and that
runs are deterministic. A core built outside `_init_core` matches every class.

## M10 — per-case tokenizer-core records, local-flow guards (superseded by M11: has the SQL-text hole below)

Commit: **9572878** (branch br-opt). Builds on M9. Docker re-run on exactly this tree
(`git archive` of the commit): valid, 0 misses, done at 126 s of the emulated run (2
verifications killed at the emulated deadline under machine load).

What changed on top of M9:
* The tokenizer-core record also exists per fixture case for the 28 per-case tests
  (4,328 cases traced alone from a warm zygote, each unioned with its test's cold
  zero-case record, as for line coverage). A tokenizer-setting edit now restricts those
  tests' verification to the cases that observe the setting. Check: for all 28 tests the
  union of the case records equals the whole test's record (0 missing line/owner pairs).
* Local flow (the truth/member rule and the core's observers) never follows a name
  declared `global`/`nonlocal` and counts `*args`/`**kwargs` as bindings (no effect on
  any local set; closes a hole for code that publishes such a local).
* Analysis indexes attribute/name mentions once per file (less analysis CPU).

| set | M9 sim | M10 sim (exact tree) | misses |
| --- | ---: | ---: | ---: |
| kit sample (30) | 96.8 | 96.8 | 0 |
| val1 (199) | 1105.5 | 1105.5 | 0 |
| val2 (120) | 552.8 | 552.8 | 0 |
| val4 (36) | 79.3 | 79.3 | 0 |
| val3 (40 class-level) | 270.6 | 270.6 | 0 |
| val5 (64 new kinds) | 203.5 | 203.5 | 0 |
| val6 (60 official-style) | 193.2 | 193.2 | 0 |
| val7 (44 tokenizer settings) | 70.8 | **47.5** | 0 |
| val8 (24 tokenizer settings, varied) | 214.9 | **202.5** | 0 |

Planner CPU estimates identical to M9 except val7 (+11.2 s local for -23.3 s simulated).
Expected on practice: same as M9 unless a tokenizer-setting edit reaches a per-case test.

## M11 — soundness fix: no text-based narrowing of SQL-keyed dicts (superseded by M12)

Commit: **34467a3** (branch br-opt). Builds on M10. Docker re-run on exactly this tree
(`git archive` of the commit): valid, 0 misses, done at 126 s of the emulated run.

Found a miss class present in every candidate so far (M1-M10): an edit of an entry of a
dict keyed by SQL text (`KEYWORDS`, `FUNCTIONS`, `*_PARSERS`, `*_MAPPING`) was narrowed to
tests whose source or data files contain the key. SQL built at runtime defeats that:
renaming the tokens.py `KEYWORDS` key `"INT"` fails
`tests/dialects/test_mysql.py::TestMySQL::test_types` (it builds `f"CAST(x AS
{signed_type.value})"`; confirmed by running it alone on the patched tree), and renaming
`"TEXT"` fails 4 tests whose text lacks the word. (New probe set `tools/gen_sqlkeys.py`,
19 key renames, pytest labels; 3 labelled so far, 2 of them would have been misses.)
M11 removes the rule: such an edit keeps every test that reads the dict through the
usual class-usage / name / typed narrowing. The class-keyed rule
(`TRANSFORMS = {exp.Foo: ...}` -> tests that built an `exp.Foo`) stays.

| set | M10 sim | M11 sim | misses |
| --- | ---: | ---: | ---: |
| kit sample (30) | 96.8 | 96.8 | 0 |
| val1 (199) | 1105.5 | 1203.3 | 0 |
| val2 (120) | 552.8 | 564.7 | 0 |
| val4 (36) | 79.3 | 79.3 | 0 |
| val3 (40 class-level) | 270.6 | 270.6 | 0 |
| val5 (64 new kinds) | 203.5 | 218.9 | 0 |
| val6 (60 official-style) | 193.2 | 193.2 | 0 |
| val7 (44 tokenizer settings) | 47.5 | 47.5 | 0 |
| val8 (24 tokenizer settings, varied) | 202.5 | 202.5 | 0 |
| val9 (60 more official-style, pytest labels, new) | n/a | 242.5 (oracle 160.9) | 0 |
| val10 = the 19 SQL-key probes (pytest labels) | 11 of 19 commits missed | 0 commits missed | |

Expected on practice: unchanged unless the batch edits a SQL-keyed dict entry (none in the
kit sample or val6); then a few to ~25 s more per such commit instead of a possible 300 s
miss. A precise replacement (runtime record of the keys each test looks up) is the next
step.

## M12 — soundness fix: module-level names read through module aliases (superseded by M13)

Commit: **daff9bc** (branch br-opt). Builds on M11. Docker re-run on exactly this tree
(`git archive` of the commit): valid, 0 misses, done at 126 s of the emulated run.

Second miss class found by adversarial probes, present in every earlier candidate: a
module-level binding of NAME in module M was only seen through `M.NAME` with M spelled by
its last name, or through names imported from M or modules re-exporting it by name. Code
that reaches M under an alias (`from sqlglot import expressions as exp`, then
`exp.UNWRAPPED_QUERIES`; the expressions package re-exports its submodules by star import)
was not a reader. Probe set `tools/gen_modrefs.py` (9 edits of module constants of the
expressions package, pytest labels): M11 misses 3 commits (PERCENTILES: test_duckdb;
CONSTANTS: 3 tests; UNWRAPPED_QUERIES: 22 tests), M12 none. M12 counts every attribute
access `<anything>.NAME`, every use of a name bound by `from <anything> import NAME [as
A]`, bare uses in files with a star import and string constants mentioning NAME (also
`getattr(m, "NAME")`, which the AST-only rule missed); module identity is not tracked.

| set | M11 sim | M12 sim (exact tree) | misses |
| --- | ---: | ---: | ---: |
| kit sample (30) | 96.8 | 96.8 | 0 |
| val1 (199) | 1203.3 | 1203.3 | 0 |
| val2 (120) | 564.7 | 564.7 | 0 |
| val4 (36) | 79.3 | 79.3 | 0 |
| val3 (40 class-level) | 270.6 | 302.0 | 0 |
| val5 (64 new kinds) | 218.9 | 218.9 | 0 |
| val6 (60 official-style) | 193.2 | 193.2 | 0 |
| val7 / val8 (tokenizer settings) | 47.5 / 202.5 | 47.5 / 202.5 | 0 |
| val9 (60 official-style) | 242.5 | 242.5 | 0 |
| modrefs probes (9) | 3 commits missed | 0 missed | |
| new-file probes (5: package shadowing a module, new fixture) | 0 missed | 0 missed | |

Also: measurement switches `BR_PROBES=0` / `BR_CLASS_USAGE=0` (defaults unchanged; used
for the audit below).

## Narrowing rules: what guarantees each one (audit at M12)

"Proof" = holds by construction given deterministic test runs at the pinned commit and
tests that do not share state through the process; "heuristic" = an argument with known
counterexamples or none, kept because no labelled commit broke it. Sets: sample = kit
sample (30); val1-val10 = our labelled sets (pytest labels except the older parts of
val1/val2/val4, re-checked where noted); probes = the adversarial sets of this round.

| # | rule (where) | guarantee | exercised by |
| --- | --- | --- | --- |
| 1 | function-body edit -> tests that ran a changed line or entered the function (`candidates._python`) | proof: per-test line + entry coverage from a fresh interpreter; a run that executes no changed line is identical | sample, val1, val2, val6, val9 (code) |
| 2 | unchanged AST (comments, formatting) -> nothing (`analyze`) | proof except line numbers; no test or library code reads sqlglot source lines, `__doc__`, `co_firstlineno` (grep) | comment kinds in val1/val2/val6/val9 |
| 3 | docstring edits -> nothing | proof for this suite: the only reader of a docstring is `expressions/core.py:112` (None -> "" normalisation); no test reads one (grep) | val1 (6), val2 (5) docstring kinds |
| 4 | no-op statements (`pass`, bare constants, `NAME = const` never mentioned elsewhere in a function without introspection) -> nothing | proof (static) | val5 dead-code kinds |
| 5 | import-time edit -> tests that executed the statement, or imported the module when the statement is not covered | proof: class/module bodies run at import; import-time lines are in each test's coverage | val3, val2, val7, val8, val10, modrefs |
| 6 | import check: an import-time value not provably safe -> the verifier imports the module; failure -> every importer | proof (runs the import) | val5 raising class body, val10 (MAP/ARRAY/CAST renames: 1020 failing each, all kept) |
| 7 | class body: AND tests that used the class (instantiated it or a subclass, entered a method; a dialect and its tokenizer/parser/generator count as one) **or ran a line reading an attribute through the class (or a subclass) spelled by name** (M13, `tools/add_xreads.py`) | argument: an attribute of C is read through an instance (instantiated), in C's methods (entered), through C's name (recorded line), or through a class object held as a value; the last is covered only where the holder is the dialect family (`self.dialect.parser_class.X`) or the metaclass at import (base coverage); a class object fetched from a registry and read without an instance would still be missed (none found by grep) | val3, val7, val8, enum and cross-class probes (val14: M12 missed 3 tests of one commit, M13 none); without this rule: sample +36.9 s, val6 +167 s, val9 +134 s |
| 8 | class body: AND lines that mention the name (text over sqlglot/, tests/, sqlglotc/, string constants included) or read attributes by computed name (getattr/hasattr/setattr/delattr with non-literal names, vars, getmembers, `__dict__`; regex of matchable names) | proof modulo computed reads outside that list (`operator.attrgetter`, `getattr_static`, `__getattribute__`: none in sqlglot or tests, grep) | val3, val2, val7, val8 |
| 9 | typed narrowing: a `self.NAME`/`cls.NAME`/`self.dialect.NAME` line counts only for receivers whose MRO contains the edited class | proof: per-test receiver records; lines without a record keep every test that ran them | val2, val3, val6 |
| 10 | tokenizer settings through `TokenizerCore` (M9/M10) | proof: static plumbing + per-test and per-case owner records of the observing core lines; off if the core, `_init_core` or any `TokenizerCore` mention changes | val7 (44), val8 (24), sample 4d63 |
| 11 | truth-only / key-membership-only reads dropped when old and new values have the same truth / dict keys | proof (static AST, local flow without global/nonlocal) | val7, val8, sample 4d63 |
| 12 | `NAME: annotation` without a value is not a read (module level, plain classes) | proof (binds nothing; dataclass-like classes excluded) | val7, val8 |
| 13 | module-level name: every `<anything>.NAME`, uses of names bound by `from <anything> import NAME [as A]`, bare uses in star-importing files, string constants; computed-name module readers of the same package; class-only readers skipped for plain values; computed reads in a function count only tests that ran it | proof modulo the computed-read list (row 8) | modrefs probes, val3, val6 (7bef) |
| 14 | class-keyed dict entry (`TRANSFORMS = {exp.Foo: ...}`) -> tests that built an `exp.Foo` or subclass | **heuristic**: such dicts are consulted by expression class; an explicit `D[exp.Foo]` lookup for another type would break it (none found by grep) | val1/val2 TRANSFORMS edits |
| 15 | dict entries (string / enum / class keys): M11 removed the text rule; M15/M17 narrow by recorded key lookups when the state fingerprints show that only recorded dict entries changed | proof modulo the recording: lookups through the dict object are recorded (RecDict overrides every read path Python code uses; no sqlglot or test code checks `type(d) is dict`, grep); dicts reachable another way (3) are never narrowed; import-time derivations are fingerprinted | val10 (19 probes, 0 missed), val1/val2/val5/val8 |
| 16 | test class: only `test*` methods changed -> those tests | proof for this suite: no test calls another test method, and no test class inherits test methods from another (checked over tests/) | val1/val2 test kinds |
| 17 | data file -> tests that opened it (Python `open` audit hook; gzip/csv go through it) | proof for this suite: tests read repository files through Python `open` (checked test_executor, helpers, jsonpath) | fixture kinds in val1/val2/val6/val9 |
| 18 | `.sql` fixture edit touching only empty or `--` lines -> nothing | proof: every `.sql` fixture is read through `tests/helpers.py`, which drops exactly those lines (same `splitlines` filter) | val1/val6 fixture comments |
| 19 | new file -> directory listers + lines mentioning the module's last name | **heuristic**: a module imported by a computed name that no line spells out would be missed | new-file probes (package shadowing), val1/val5 newfile kinds |
| 20 | deleted / renamed / binary file -> openers, importers, its own tests, listers; mode-only -> nothing | proof | val2/val5, edge set |
| 21 | pytest configuration -> everything (conftest: tests below it); unparsable patch or unknown file -> everything | proof | val5, edge set |
| 22 | verification: candidates that pass when run here with the patch are left out | proof modulo environment (in-process unittest semantics, process pools replaced by a synchronous executor, generous limits) | every set |
| 23 | per-case restriction: only the cases that reach the change (code) or changed (fixture) | proof modulo state carried between cases; union of case coverage = whole-test coverage for all 28 tests | val1/val2/val6 optimizer and TPC edits |
| 24 | TPC-DS stage check: optimizer outputs equal and nothing outside `optimize()` reached -> rest of each case unchanged | proof modulo determinism (fingerprints recorded under two hash seeds) | sample 2jrq, val2/val6 TPC-DS candidates |
| 25 | infection probes: unrun candidates (incl. expensive ones) left out when the edited expressions never evaluate differently in the cheap tests run (>= 50 evaluations, >= 3 tests) | **heuristic** (induction from the tests that ran) | 25 labelled "inert" decisions, all right: sample 2, val4 11 (built for it), val15 = 40 is/==-swap edits 12 (0 misses on val15 overall); switching it off (`BR_PROBES=0`) costs sample +33.0 s, val4 +158.8, val2 +3.5, val1 +2.0, val6/val9 +0 |

Determinism check behind rows 1, 5, 9, 10, 23: 161 tests (every 10th, under 3 s) traced
under PYTHONHASHSEED=1 and =2: identical line coverage for all 161.

## M13 — soundness fix: class attributes read through the class name (superseded by M14)

Commit: **e58bf21** (branch br-opt). Builds on M12. Docker re-run on exactly this tree
(`git archive` of the commit): valid, 0 misses, done at 126 s of the emulated run.

Third miss class, found by the audit of row 7: the class-attribute rule kept only tests
that used the class, but a read through the class spelled by name needs no instance.
Presto's class body takes `TIME_MAPPING = MySQL.TIME_MAPPING` (the same dict), Athena's
tokenizer merges Hive's and Trino's settings, PrestoGenerator reads
`Hive.INVERSE_TIME_MAPPING`. Changing MySQL.TIME_MAPPING's "%i" entry fails
test_presto::test_to_char, test_hive::test_time and test_spark::test_to_date; M12 left
them out. `tools/add_xreads.py` records (from the live modules of the pinned checkout)
every attribute access through a class spelled by name: 2,167 reads, receivers bound by
function-local imports resolved by name. Tests that ran such a line for the edited class
or a subclass now count as its users. Probe set val14 (6 cross-class edits, pytest
labels): M12 misses 1 commit, M13 none.

| set | M12 sim | M13 sim | misses |
| --- | ---: | ---: | ---: |
| kit sample (30) | 96.8 | 96.8 | 0 |
| val1 (199) | 1203.3 | 1225.4 | 0 |
| val2 (120) | 564.7 | 608.0 | 0 |
| val4 (36) | 79.3 | 79.3 | 0 |
| val3 (40 class-level) | 302.0 | 324.2 | 0 |
| val5 (64 new kinds) | 218.9 | 238.6 | 0 |
| val6 (60 official-style) | 193.2 | 193.2 | 0 |
| val7 / val8 (tokenizer settings) | 47.5 / 202.5 | 51.0 / 266.0 | 0 |
| val9 (60 official-style) | 242.5 | 250.3 | 0 |
| val10-13 (SQL-key, module-alias, new-file, enum probes) | 0 missed | 0 missed | 0 |
| val14 cross-class probes (6) | 1 commit missed | 0 missed | |

Expected on practice: unchanged for sample-like commits (sample and val6 identical); more
for class-level edits of dialect/tokenizer settings that other classes read by name.

Decision left to the coordinator: row 25 is the one rule whose evidence is statistical.
25 of 25 labelled inert decisions were right (95% upper bound on the error rate about
12%); it saves 33 s on the kit sample (2 of 30 commits). `BR_PROBES=0` in the Dockerfile's
environment (or flipping the default in `selector/policy.py`) turns it off with no other
change.

## M14 — class-name reads count only for the edited attribute (superseded by M15)

Commit: **19278c8** (branch br-opt). Builds on M13 (keeps all three soundness fixes).
Docker re-run on exactly this tree (`git archive` of the commit): valid, 0 misses, done
at 124 s of the emulated run.

M13 counted every read through the edited class's name; a read of another attribute
(`MySQL.TIME_FORMAT` for an edit of `MySQL.TIME_MAPPING`) cannot read the edited one
(nested chains are recorded per class: `MySQL.Tokenizer.KEYWORDS` is a read of
`Tokenizer` through MySQL and of `KEYWORDS` through `MySQL.Tokenizer`). The table now
keeps the attribute. Probe set val14: still 0 missed.

| set | M13 sim | M14 sim | misses |
| --- | ---: | ---: | ---: |
| kit sample (30) | 96.8 | 96.8 | 0 |
| val1 (199) | 1225.4 | 1203.3 | 0 |
| val2 (120) | 608.0 | 583.5 | 0 |
| val4 (36) | 79.3 | 79.3 | 0 |
| val3 (40 class-level) | 324.2 | 302.0 | 0 |
| val5 (64 new kinds) | 238.6 | 238.6 | 0 |
| val6 (60 official-style) | 193.2 | 193.2 | 0 |
| val7 / val8 (tokenizer settings) | 51.0 / 266.0 | 47.5 / 266.0 | 0 |
| val9 (60 official-style) | 250.3 | 242.5 | 0 |
| val10-13 probes | 0 missed | 0 missed | 0 |
| val14 cross-class probes (6) | 0 missed | 0 missed | |

## M15 — key-level records for string-keyed dict edits (superseded by M17)

Commit: **f72a919** (branch br-opt; first version c335a50, same results). Docker re-run
on exactly this tree (`git archive`): valid, 0 misses, done at 126 s of the emulated run;
in the container the sample's 4d63 is narrowed by its key record ("keys: 1 entries of 1
dicts").

M11 made every edit of a `KEYWORDS`/`FUNCTIONS`/`*_PARSERS`/`*_MAPPING` entry keep every
test that reads the dict; M13 then also kept everything reached through class-name
copies (`Presto.TIME_MAPPING = MySQL.TIME_MAPPING`). M15 replaces that with runtime
evidence:
* build time (`tools/trace_keys.py`): after importing every sqlglot module, every
  string-keyed dict held by a sqlglot class or module is swapped for a recording copy
  (aliases stay aliases; the 3 dicts still reachable another way, e.g. through a dict
  view, are "unrecordable" and never narrowed); each test runs alone and records the
  keys it looked up and the dicts it enumerated (1620/1620 tests);
* decision time (`selector/keyrefine.py`): the state of every sqlglot class and module
  is fingerprinted for the pinned tree (two hash seeds, stored in the image at build
  time) and for the patched tree (one subprocess per such commit, ~1.4 s local CPU), so
  every dict entry the edit changes is known, including import-time derivations
  (subclass copies, inverted mappings, tries, cross-class copies). Only if every
  difference is an entry of a recorded dict are the site's candidates cut to the tests
  that looked up a changed key or enumerated a changed dict, plus every test without a
  record; anything else keeps M14's candidates.
* Soundness argument: an entry edit can only change a test's run where the test reads
  that entry (a recorded lookup), reads the dict whole (a recorded enumeration), or
  uses state derived at import time (all fingerprinted: any non-dict difference stops
  the narrowing). Edits that move entries are not narrowed (no order changes possible).

| set | M8 | M13 (submitted: 162.5 sim on practice) | M14 | **M15** | misses |
| --- | ---: | ---: | ---: | ---: | ---: |
| kit sample (30) | 104.8 | 96.8 | 96.8 | **96.8** | 0 |
| val2 (120) | 581.4 | 608.0 | 583.5 | **538.5** | 0 |
| val6 (60 official-style) | 193.2 | 193.2 | 193.2 | **193.2** | 0 |
| val9 (60 official-style) | 292.0 | 250.3 | 242.5 | **242.5** | 0 |
| val1 (199) | 1105.5 | 1225.4 | 1203.3 | **1077.5** | 0 |
| val5 (64 new kinds) | 203.5 | 238.6 | 238.6 | **180.7** | 0 |
| val8 (24 tokenizer settings) | 402.6 | 266.0 | 266.0 | **236.3** | 0 |
| val10 (19 SQL-key probes) | 11 commits missed | 662.7 | 662.7 | **361.5** | 0 |
| val11 / val14 (module-alias / cross-class probes) | misses | 169.4 / 130.1 | 169.4 / 130.1 | **140.7 / 94.9** | 0 |
| val3, val4, val7, val12, val13, val15 | | | | unchanged from M14 | 0 |

On every set M15 is at or below M14, and on val1/val2/val5 below even M10's unsound text
rule. Not covered by M15: dicts keyed by enum members or classes (`TYPE_MAPPING`,
`TRANSFORMS`; the latter has its own class rule), module-level name reads (M12) and
non-dict class attributes copied across classes (M13) — next.

## M17 — dicts keyed by enums/classes, recorded module attribute reads (recommended next; includes M7-M15)

Commit: **2963170** (branch br-opt; M16 = 7dac690 is the intermediate with only the first
change). Docker re-run on exactly this tree (`git archive`): valid, 0 misses, done at 126 s
of the emulated run (4d63 narrowed by its key record in the container).

* Key records also for dicts keyed by enum members and classes (`TYPE_MAPPING` with
  `DType` keys, TokenType-keyed parser tables): keys named by qualified name.
* Module-level value edits (replacing M12's "any `<x>.NAME` on a line the test ran"):
  `tools/trace_modattrs.py` swaps each sqlglot module object's class for a recording
  ModuleType subclass in a zygote that imported everything, and each test records the
  attributes it read through module objects (1620/1620). Attribute reads inside
  functions now count only for tests without a record; tests with one count when they
  read a binding holding the value (the name and its from-import/star re-exports,
  searched under alias names too). Bare-name uses, import-time lines, string constants
  and computed-name readers keep the static rule. Applied only when the state
  fingerprint shows that nothing but those bindings (and recorded dict entries)
  changed.
* Soundness argument: a function can read the module binding only by a bare name
  (static rule kept), through a module object (recorded), or through state built at
  import (fingerprinted: any other change keeps the M12 mask).

| set | M8 | M13 (practice 162.5 sim) | M15 | **M17** | misses |
| --- | ---: | ---: | ---: | ---: | ---: |
| kit sample (30) | 104.8 | 96.8 | 96.8 | **96.8** | 0 |
| val2 (120) | 581.4 | 608.0 | 538.5 | **532.8** | 0 |
| val6 (60 official-style) | 193.2 | 193.2 | 193.2 | **193.2** | 0 |
| val9 (60 official-style) | 292.0 | 250.3 | 242.5 | **242.5** | 0 |
| val1 (199) | 1105.5 | 1225.4 | 1077.5 | **1071.1** | 0 |
| val3 (40 class-level) | 276.9 | 324.2 | 302.0 | **297.8** | 0 |
| val5 (64 new kinds) | 203.5 | 238.6 | 180.7 | **176.4** | 0 |
| val8 / val10 / val11 / val14 | 402.6 / misses / misses / misses | 266.0 / 662.7 / 169.4 / 130.1 | 236.3 / 361.5 / 140.7 / 94.9 | **236.3 / 362.6 / 138.7 / 94.9** | 0 |
| val16 = 60 new class-level edits (pytest labels) | | 596.6 | | **546.7** | 0 |
| val4, val7, val12, val13, val15 | | | | unchanged | 0 |

Still widened (M13): non-dict class attributes read through another class's name or
copied across classes; next candidate.

Practice result for M17 (2963170, public a394dc5): **166.2 s = 140.205 simulated +
25.78-26.06 CPU, 0 missed** (M13: 162.513 + 16.2-16.8 = 179.2; M8: 137.985 + 14.5).

## M18 — class-body edits narrowed through the state comparison (superseded by M19)

Commit: **daf0f33**. Not Docker-checked on its own (M19 builds on it and was).

* For a class-body assignment, keyrefine compares the whole program state (pinned vs
  patched). When every difference is a class attribute of a class in the class map
  (the edited one or one derived from it at import time, e.g. `Presto.TIME_MAPPING =
  MySQL.TIME_MAPPING` shows up as its own changed attribute), a recorded dict entry or
  the edited module binding, the site's candidates become the runtime readers of each
  changed attribute: the M13/M14 class-attribute rule (class users or reads through
  the class name, AND lines mentioning the name, typed where recorded, computed-name
  readers) with every module- and class-level line left out. Anything else keeps the
  M14 mask.
* Soundness argument: an import-time reader of the attribute can only matter through
  what it stores, and everything stored in sqlglot classes and module globals is in
  the comparison (any such difference must itself be narrowable, or the M14 mask
  stays); runtime readers are covered by the M13/M14 rule. Residual: state stored
  outside sqlglot classes/modules (no `lru_cache` or similar in sqlglot, grep).

| set | M17 | M18 | misses |
| --- | ---: | ---: | ---: |
| kit sample | 96.8 | 96.8 | 0 |
| val16 (60 class-level, pytest labels) | 546.7 | 546.3 | 0 |
| val3 (40 class-level) | 297.8 | 287.5 | 0 |
| val9 / val6 / val2 | 242.5 / 193.2 / 532.8 | 242.3 / 193.2 / 532.8 | 0 |
| val8 / val14 / val1 | 236.3 / 94.9 / 1071.1 | 171.6 / 54.4 / 1060.3 | 0 |
| val4, val5, val7, val10-val13, val15 | | unchanged | 0 |

## M19 — state comparison at half the CPU, only when it pays; seed-aware order reading (recommended next; includes M7-M18)

Commit: **e73afb6**. Docker re-run on exactly this tree (`git archive`, `docker build
--no-cache`, from-scratch build **204 s**): valid, 0 misses, simulated 121.616 s (as
M17), done at 126 s of the emulated run; 3 state comparisons ran in the container.

* `keyrec.fp` memoises fingerprints inside the fingerprinting process and skips the
  order-insensitive pass for values without a list/tuple of 2+ items. Ordered
  fingerprints are identical to M17's for all 17643 entries. One fingerprint child:
  1.84-1.90 s CPU (M17) -> 0.75-0.89 s (M19), measured back to back.
* The image stores one pinned fingerprint plus the 3 values whose order differs
  between two hash seeds (file 2.0 MB -> 1.0 MB); the patched overlay no longer
  copies the test files.
* Gate (`policy.state_check_worthwhile`): the comparison runs only when the tests that
  nothing but narrowable import-time sites select would cost more to handle (the
  cheaper of packed inclusion and verification CPU) than its 0.8 s. Skipping only
  keeps the wider masks.
* **Soundness fix (also applies to M17's paths):** a non-dict value whose
  order-insensitive fingerprint matched used to count as unchanged, so a patch whose
  only import-time effect is reordering a list or tuple (directly or derived) could
  have been missed. Now that reading applies only to the 3 values whose order
  already differs between hash seeds on the pinned commit (built from sets), and the
  patched tree is fingerprinted under the same seed as the stored pinned run. No
  validation commit exercised the gap (no miss before or after).

Kit-sample CPU, same units (user+sys of the whole local selector run, children
included, native M1, pinned fingerprint precomputed as in the image, interleaved;
round 1 discarded as warm-up):

| | M13 | M17 | M19 |
| --- | ---: | ---: | ---: |
| total CPU (rounds 2, 3) | 10.12, 9.89 | 12.70, 12.86 | 10.85, 10.92 |
| verification CPU | 8.56, 8.33 | 7.50, 7.61 | 7.40, 7.47 |
| rest (analysis + state comparison) | 1.56, 1.56 | 5.20, 5.25 | 3.45, 3.45 |
| state comparisons run | 0 | 3 | 3 |

So the comparison's cost on the sample fell from about 3.6 s to about 1.9 s (rest
minus M13's analysis), and the narrowing pays back about 1 s of verification CPU. On
practice M17 cost +9.5 s CPU over M13; at the same number of comparisons M19 should
cost roughly half that (not yet measured on the scorer).

| set | M17 | M19 | misses |
| --- | ---: | ---: | ---: |
| kit sample | 96.8 | 96.8 | 0 |
| val1 | 1071.1 | 1060.3 | 0 |
| val2 | 532.8 | 532.8 | 0 |
| val3 | 297.8 | 287.5 | 0 |
| val4 | 79.3 | 79.3 | 0 |
| val5 | 176.4 | 176.4 | 0 |
| val6 | 193.2 | 193.2 | 0 |
| val7 | 47.5 | 47.5 | 0 |
| val8 | 236.3 | 166.3 | 0 |
| val9 | 242.5 | 242.3 | 0 |
| val10 | 362.6 | 362.6 | 0 |
| val11 / val12 / val13 | 138.7 / 138.7 / 110.5 | 138.7 / 138.7 / 110.5 | 0 |
| val14 | 94.9 | 46.5 | 0 |
| val15 | 169.9 | 169.9 | 0 |
| val16 | 546.7 | 546.3 | 0 |

## M20 — lambda bodies inside comprehensions are function-scope changes

Commit: **2215534** (on M19). Not Docker-checked on its own (M21 builds on it and was).

* `TRANSFORMS = {k: v for k, v in {..., exp.SHA2: lambda self, e: ...}.items() if ...}`:
  the AST diff stopped at the comprehension clause (no line number) and reported the
  whole class-body statement, so an edit inside one lambda body widened to every test
  that used the dialect (val9 qh47: 270 tests). The diff now descends into nodes
  without line numbers; the only consumer is the "every difference sits in a lambda
  body" check, whose sites use line and entry coverage of the lambda (proof).
* val9 242.3 -> **235.1**; every other set unchanged; 0 misses everywhere.

## M21 — the state comparison also requires the same import-time imports and dict key order (includes M7-M20)

Commit: **55d8c1b**. Docker: see below.

Two gaps in what "nothing else changed" meant for the M15/M17/M18 narrowing (no
validation commit hit either; found by reading the code):
* Tests import only part of sqlglot, but the fingerprints are taken after importing
  everything. A patch that changes which imports run at import time (and so what gets
  registered) could change what a test sees without changing the all-modules state.
  The fingerprint now records every import executed by sqlglot code while importing
  (importer, module, names: 1202 edges, identical across hash seeds); any difference
  keeps the wider masks.
* Dict entries were compared as mappings, so a dict whose keys only changed order
  looked unchanged. Now a different relative order of the common keys reports the
  dict as changed for its enumerators, except for the 50 dicts whose order already
  differs between two hash seeds on the pinned commit (all `EXPRESSION_METADATA`
  tables, two ClickHouse and one Exasol parser table: built from sets, so tests cannot
  rely on their order).

Every set has the same simulated seconds as M20 (sample 96.8, val1 1060.3, val2 532.8,
val3 287.5, val4 79.3, val5 176.4, val6 193.2, val7 47.5, val8 166.3, val9 235.1,
val10 362.6, val11 138.7, val12 138.7, val13 110.5, val14 46.5, val15 169.9, val16
546.3), 0 misses on every set.

### New kinds of edit (tools/gen_kinds2.py, labelled by real pytest)

Deleted and renamed functions (alone, or with every reference in sqlglot), changed
defaults, added/removed parameters, added/removed decorators, `__init__` re-exports
removed, added tests (passing/failing), trailing whitespace, multi-file mixes:
47 commits (`.work/labels/kinds2`, not in the image). Labelling in progress; see the
latest note below for counts.

### Infection-probe evidence

Official-style edits almost never reach a probe decision: on 174 fresh official-style
code mutants (seed 4242) the selector chose a probe plan 8 times and judged none inert
(val6: 0 probe plans, val9: 1, not inert). Inert decisions come from `is`/`==`-type
swaps, so the new evidence is a fresh probe-edit set (`tools/gen_probe_edits.py`, seed
777, 73 commits after removing 7 already in val15): 42 probe plans, 22 inert decisions,
each labelled by real pytest: **22 right, 0 wrong** (all 0 failing). With the earlier
25 (sample, val4, val15): 47 of 47 labelled inert decisions right. One probe edit
(p149, `start_hash == end_hash` -> `is` in `helper.while_changing`) makes every
optimizer test loop forever; the selector's light probe timed out ("verification
unavailable") and it included all 121 candidates.

New kinds (kinds2), labelled so far: 13 of 47, 0 commits with a miss (M20 selection).

## CPU diagnosis (after M19's practice score)

Clean interleaved local runs, exact trees, pinned fingerprint precomputed as in the
image, labellers paused; CPU = user+sys of the whole selector run (native M1):

| set | M13 total (verification) | M17 | M19 | M19 with state comparison off |
| --- | --- | --- | --- | --- |
| val9 (60 official-style) | 12.26 (9.76), sim 250.3 | 17.83 (8.20), sim 242.5 | 17.92 (8.21), sim 242.3 | 11.31, sim 242.5 |
| val6 (60 official-style) | 20.77 (18.26), sim 193.2 | 28.89 (18.37), sim 193.2 | 26.41 (18.27), sim 193.2 | 21.55, sim 193.2 |
| kit sample (30) | 9.74 (8.14), sim 96.8 | 12.69 (7.45), sim 96.8 | 10.93 (7.48), sim 96.8 | 9.69, sim 96.8 |
| val5 (64 new kinds) | 5.40 (3.19), sim 238.6 | 10.65 (1.36), sim 176.4 | 7.12 (1.33), sim 176.4 | 5.34, sim 238.6 |

* Verification CPU did not grow from M13 to M17/M19 (flat or lower everywhere): the
  extra CPU is the state comparison. Non-verification CPU minus M13's, divided by the
  comparisons run: M17 1.18-1.34 s each (6 per 60 official-style commits), M19
  0.60-0.70 s each (8-12 per 60 commits, because M18 adds class-body sites). That is
  why M19 saved only ~1 s on practice.
* On official-style sets the comparisons buy almost nothing: val9 0.2 s, val6 0,
  sample 0 simulated, for 4.9-6.6 s local CPU per 60 commits. They pay on dict-entry
  kinds (val5: 62 s for 1.8 s CPU; val1, val2).
* So M13 -> M17's -22.3 s on practice need not come from the comparisons: M14 alone
  took val9 from 250.3 to 242.5. Scorer/local CPU ratio implied by M13 -> M17 (+9.4 s
  scorer, about +5 s local per 40 official-style commits): about 1.9 for this work.

## M22 — the state comparison runs only when planning with its likely result pays (recommended next; includes M7-M21)

Commit: **52f588a**. Docker re-run on exactly this tree (`git archive`, `docker build
--no-cache` 195 s): valid, 0 misses, simulated 96.839 s, done at 93 s of the emulated
run (M19/M21: 121.616 s, done at 126 s).

* Gate (`policy.state_check_worthwhile`): computes, without fingerprints, the masks a
  confirming comparison would leave (key records of the edited keys plus enumerators
  and untraced tests; the module record alternative; the runtime readers of assigned
  class attributes), plans the commit with and without them, and runs the comparison
  only if the planner's expected cost drops by more than 0.65 s local x 1.9 = 1.23 s.
  Skipping keeps the wider masks (sound); the comparison itself still decides.
* `CPU_SCALE` for verification is unchanged (1.0): the five practice points cannot
  separate verification CPU from comparison CPU (verification did not change between
  M13 and M17 locally, so the 1.9 is a property of the comparison's cost), whole-run
  ratios on local proxies range 1.4-1.9 for an unknown practice mix, and local scoring
  cannot measure the trade-off. Not changed without evidence.
* The container-order fix (padded build-time runs) is **not** included: inside the
  image the five parallel build runs failed ("pinned fingerprint failed"), so it is
  not validated.

| set | M21 sim | M22 sim | comparisons M21 -> M22 | misses |
| --- | ---: | ---: | --- | ---: |
| kit sample | 96.8 | 96.8 | 3 -> 1 | 0 |
| val9 / val6 (official-style) | 235.1 / 193.2 | 235.1 / 193.2 | 11 -> 0 / 8 -> 0 | 0 |
| val5 / val10 | 176.4 / 362.6 | 176.4 / 362.6 | 6 -> 6 / 19 -> 19 | 0 |
| val1 | 1060.3 | 1073.2 | 22 -> 8 | 0 |
| val2 / val3 / val8 | 532.8 / 287.5 / 166.3 | 532.8 / 287.5 / 166.3 | 22 -> 10 / 23 -> 3 / 22 -> 7 | 0 |
| val16 | 546.3 | 546.7 | 46 -> 9 | 0 |
| val4, val7, val11-val15 | | unchanged | | 0 |

Local CPU, same units as the diagnosis (interleaved exact trees, user+sys of the run):

| set | M13 CPU / sim | M19 CPU / sim | M22 CPU / sim |
| --- | --- | --- | --- |
| kit sample | 9.73 / 96.8 | 10.95 / 96.8 | 9.98 / 96.8 |
| val9 | 12.10 / 250.3 | 17.86 / 242.3 | 10.63 / 235.1 |
| val6 | 20.87 / 193.2 | 26.41 / 193.2 | 21.54 / 193.2 |

M22 is back to M13's CPU with M19's (val9: M20's better) simulated cost. If practice
behaves like these sets, expect roughly M13's CPU (16-17 s) with about 140 s
simulated; not yet measured on the scorer.

A labelled kinds2 commit (bipi, trailing whitespace) showed test_execute_tpcds failing,
which no selection includes. Rerun with real pytest on the patched tree: it passes
(57 subtests). The label is false: I had paused the labeller with SIGSTOP while it ran
that test. Not a miss; bipi will be relabelled, and the labeller is no longer paused.
