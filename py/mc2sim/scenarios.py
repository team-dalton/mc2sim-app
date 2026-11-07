"""What-if scenarios: close exits, doors or staircases and compare with the open building.

`stress_test` is a one-closure-at-a-time check (like the "discounted exit" / "discounted stair"
checks in fire codes): it closes each exit and staircase that carries a real share of the
evacuation, one at a time, and ranks the outcomes by how much worse they are than the open building.
Runs go to worker processes so several scenarios simulate at the same time.
"""
from __future__ import annotations

import dataclasses
import multiprocessing as mp
import os
from concurrent.futures import ProcessPoolExecutor, as_completed

from .analyze import Building
from .sim import SimParams, SimResult, simulate

_B: Building | None = None


def _init(b: Building):
    global _B
    _B = b


def _run(p: SimParams) -> SimResult:
    return simulate(_B, p)


def default_workers(n: int) -> int:
    """Parallel runs: leave half the cores free; each run needs a few hundred MB."""
    return max(1, min(n, (os.cpu_count() or 2) // 2 - 1, 3))


def run_many(b: Building, params: list[SimParams], progress=None, workers: int | None = None) -> list[SimResult]:
    """Simulate several parameter sets, in parallel when it helps. Results keep the input order."""
    if not params:
        return []
    workers = default_workers(len(params)) if workers is None else max(1, min(workers, len(params)))
    out: list[SimResult | None] = [None] * len(params)
    if workers == 1:
        for i, p in enumerate(params):
            out[i] = simulate(b, p)
            if progress:
                progress(i + 1, len(params))
        return out
    with ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn"), initializer=_init, initargs=(b,)) as ex:
        futs = {ex.submit(_run, p): i for i, p in enumerate(params)}
        for k, f in enumerate(as_completed(futs), 1):
            out[futs[f]] = f.result()
            if progress:
                progress(k, len(params))
    return out


def summary(r: SimResult) -> dict:
    st = r.stats
    return {k: st[k] for k in ("people", "evacuated", "trapped_no_route", "not_out_by_end", "total_time_s",
                               "t50_s", "t90_s", "mean_walk_m", "turned_back")}


def compare(base: SimResult, r: SimResult) -> dict:
    """Headline numbers of `r` and their change against `base`. Time changes are None when they are
    undefined or not comparable (someone could not get out, so "everyone out" never happened)."""
    a, s = summary(base), summary(r)
    stuck = s["trapped_no_route"] + s["not_out_by_end"]
    d = lambda k: None if stuck or s[k] is None or a[k] is None else round(s[k] - a[k], 1)
    return {**s, "delta_total_s": d("total_time_s"), "delta_t90_s": d("t90_s"), "delta_t50_s": d("t50_s"),
            "no_way_out": stuck}


def candidates(b: Building, base: SimResult, min_share: float = 0.03, max_exits: int = 6,
               max_stairs: int = 4) -> list[tuple[str, int]]:
    """Exits and staircases worth testing: those used by at least `min_share` of people when all is open."""
    st = base.stats
    need = max(1, min_share * st["people"])
    ex = sorted((e for e in st["exits"] if e["people"] >= need), key=lambda e: -e["people"])[:max_exits]
    sr = sorted((s for s in st.get("stairs", []) if s["people"] >= need), key=lambda s: -s["people"])[:max_stairs]
    return [("exit", e["id"]) for e in ex] + [("stairs", s["id"]) for s in sr]


def closing(p: SimParams, typ: str, i: int) -> SimParams:
    p0 = p.without_closures()
    key = {"exit": "closed_exits", "door": "closed_doors", "stairs": "closed_stairs"}[typ]
    return dataclasses.replace(p0, **{key: [int(i)]})


def severity(row: dict):
    """Sort key, worst first: people who cannot get out, then extra time for everyone to get out."""
    dt = row["delta_total_s"]
    return (-row["no_way_out"], -(dt if dt is not None else float("inf")))


def stress_test(b: Building, p: SimParams, baseline: SimResult | None = None, progress=None,
                workers: int | None = None):
    """Close each main exit / staircase on its own. Returns (baseline, [(row, result)] worst first)."""
    p0 = p.without_closures()
    if baseline is None:
        if progress:
            progress("Running the evacuation with everything open first…")
        baseline = simulate(b, p0)
    cands = candidates(b, baseline)
    params = [closing(p0, t, i) for t, i in cands]
    report = (lambda k, n: progress(f"{k} of {n} scenarios done")) if progress else None
    if progress:
        progress(f"0 of {len(params)} scenarios done")
    results = run_many(b, params, progress=report, workers=workers)
    rows = []
    for r in results:
        row = compare(baseline, r)
        row["closed"] = r.stats["scenario"]["closed"][0]
        rows.append((row, r))
    rows.sort(key=lambda t: severity(t[0]))
    return baseline, rows
