"""Split selected tests across workers to minimise busiest-worker load + 2 s per worker."""

from __future__ import annotations

WORKER_COST_MS = 2000
MAX_WORKERS = 8


def _lpt(durations: list[tuple[int, int]], k: int) -> tuple[int, list[list[int]]]:
    loads = [0] * k
    bins: list[list[int]] = [[] for _ in range(k)]
    for number, ms in durations:
        idx = loads.index(min(loads))
        bins[idx].append(number)
        loads[idx] += ms
    return max(loads), bins


def split(selected: set[int], ms_of: dict[int, int]) -> list[list[int]]:
    if not selected:
        return []
    durations = sorted(((n, ms_of[n]) for n in selected), key=lambda x: (-x[1], x[0]))
    best_cost, best_bins = None, []
    for k in range(1, min(MAX_WORKERS, len(durations)) + 1):
        makespan, bins = _lpt(durations, k)
        cost = makespan + WORKER_COST_MS * k
        if best_cost is None or cost < best_cost:
            best_cost, best_bins = cost, [b for b in bins if b]
    return best_bins
