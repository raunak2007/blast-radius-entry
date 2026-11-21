# Blast Radius entry

Picks the sqlglot tests each patch can break, then packs them onto workers.

**Sample set (30 commits):** 187.2 s simulated, 0 misses, about 0.3 s CPU. The starter scores 1210.5 s.

## How it decides

Every map comes from running sqlglot's own tests at the pinned commit (`eb11aec4`), which the track rules allow. No patch or answer file is used to build anything.

- **Comment-only hunks** (comments or blank lines outside strings) change nothing Python runs, so they select nothing.
- **A changed statement inside a function** selects the tests that executed it (per-test line coverage).
- **A changed class attribute, or a new method** selects the tests that used that class: the dialects it serves (and their subclasses), or the expression type and its subclasses.
- **A changed fixture or data file** selects the tests that opened it.
- **A changed test file** selects the edited tests (or their class or file).
- **Anything risky** runs every test: a patch that won't apply, a syntax error, a module-level change other modules import, config files, renames and deletions.

Tests are packed longest-first, trying 1 to 8 workers and keeping the cheapest.

## Files

| File | What it is |
| --- | --- |
| `selector.py` | The program the scorer runs (stdlib only) |
| `blast_map.pkl.gz` | Coverage, file reads, and dialect/expression usage per test |
| `sqlglot_src.tar.gz` | The pinned commit's Python sources, which patches are applied to |
| `tools/` | Rebuilds both data files from the pinned commit (`tools/build_map.sh`) |
| `Makefile`, `score.py`, `requirements.lock` | From the starter kit, unchanged |

The build needs no network beyond the base image.

## Test locally

Copy the kit's `sample/` folder in (it's git-ignored), then:

```sh
make sample
```
