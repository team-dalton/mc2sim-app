"""What the web app can ask for, independent of how the request arrives.

Two front doors use this: the local HTTP server (server.py, background threads for long jobs) and the
browser-only mode (browser.py, inside Pyodide in a Web Worker, where long jobs run in the worker and report
progress as messages). Answers are JSON-ready dicts (typed arrays as base64) or raw bytes.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict

import numpy as np

from .analyze import analyze
from .demo import make_school
from .export import b64, building_json, pack_frames, render_payload, results_json, unpack_frames
from .scenarios import compare, run_many, stress_test, summary
from .sim import SimParams, closures, simulate

MAX_AREA = 700 * 700
MAX_SIMS = 12                       # finished simulations kept in memory (oldest dropped first)
MAX_VOLUMES = 4                     # raw block volumes kept for re-analysis with corrections
MAX_BUILDINGS = 8                   # analysed buildings kept in memory
# parameters that describe the scenario rather than the people; a baseline must match on everything else
SCENARIO_KEYS = {"closed_exits", "closed_doors", "closed_stairs", "awareness", "notice_m", "tell_rate"}


def _strip(items):
    return [{k: v for k, v in it.items() if k != "nodes"} for it in items]


def _same_people(a: dict, b: dict) -> bool:
    return all(a.get(k) == b.get(k) for k in set(a) | set(b) if k not in SCENARIO_KEYS)


def _opt_int(v):
    return None if v in (None, "") else int(v)


def selection(body: dict) -> tuple[int, int, int, int, int]:
    """(x0, z0, x1, z1, pad) of a convert request, checked against the size limit."""
    x0, z0, x1, z1 = (int(body[k]) for k in ("x0", "z0", "x1", "z1"))
    pad = int(body.get("pad", 6))
    x0, x1 = sorted((x0, x1))
    z0, z1 = sorted((z0, z1))
    if (x1 - x0 + 1 + 2 * pad) * (z1 - z0 + 1 + 2 * pad) > MAX_AREA:
        raise ValueError("Selection is too large for the prototype (max ~690 x 690 blocks). "
                         "Select one building or campus section at a time.")
    return x0, z0, x1, z1, pad


def regions_for(body: dict) -> list[tuple[int, int]]:
    """Region files (rx, rz) a convert request reads: the selection plus its padding."""
    x0, z0, x1, z1, pad = selection(body)
    return [(rx, rz) for rz in range((z0 - pad) >> 9, ((z1 + pad) >> 9) + 1)
            for rx in range((x0 - pad) >> 9, ((x1 + pad) >> 9) + 1)]


class App:
    """Analysed buildings and finished runs, kept in memory (oldest dropped first)."""

    def __init__(self):
        self.buildings: dict = {}
        self.volumes: dict = {}
        self.sims: dict = {}
        self.lock = threading.Lock()

    # ------------------------------------------------------------ buildings ---
    def building(self, bid: str):
        if bid not in self.buildings:
            raise KeyError("This building is no longer in memory - convert it again.")
        return self.buildings[bid]

    def new_building(self, vol, ymin, ymax, fixes, source) -> dict:
        """Analyse a volume (with corrections), remember it, and build the browser response."""
        t = time.time()
        b = analyze(vol, ymin=ymin, ymax=ymax, fixes=fixes)
        bid = uuid.uuid4().hex[:8]
        source = {**source, "corrections": len(b.fixes)}
        with self.lock:
            self.buildings[bid] = (b, source)
            self.volumes[bid] = (vol, ymin, ymax, source)
            while len(self.volumes) > MAX_VOLUMES:
                self.volumes.pop(next(iter(self.volumes)))
            while len(self.buildings) > MAX_BUILDINGS:
                self.buildings.pop(next(iter(self.buildings)))
        return {
            "id": bid, "source": source, "summary": b.summary(), "levels": b.levels, "rooms": b.rooms,
            "doors": _strip(b.doors), "exits": _strip(b.exits), "stairs": _strip(b.stairs),
            "objects": b.objects, "signs": b.signs, "fixes": b.fixes, "render": render_payload(b),
            "convert_s": round(time.time() - t, 2),
        }

    def convert(self, world, body: dict) -> dict:
        """Read the selected box (+ padding) from a World and analyse it."""
        x0, z0, x1, z1, pad = selection(body)
        vol = world.extract(x0 - pad, z0 - pad, x1 + pad, z1 + pad)
        ymin, ymax = body.get("ymin"), body.get("ymax")
        source = {"world": body["world"], "mc_version": world.info.get("version"),
                  "selection": {"x0": x0, "z0": z0, "x1": x1, "z1": z1, "pad": pad, "ymin": ymin, "ymax": ymax}}
        return self.new_building(vol, _opt_int(ymin), _opt_int(ymax), body.get("fixes"), source)

    def demo(self, body: dict) -> dict:
        source = {"world": "(built-in demo building)", "mc_version": None, "selection": None}
        return self.new_building(make_school(with_markers=True), None, None, body.get("fixes"), source)

    def reanalyze(self, body: dict) -> dict:
        """Same blocks, new corrections (from the floor plan)."""
        if body["id"] not in self.volumes:
            raise KeyError("This building's blocks are no longer in memory - convert it again.")
        vol, ymin, ymax, source = self.volumes[body["id"]]
        return self.new_building(vol, ymin, ymax, body.get("fixes") or [], source)

    def export_building(self, bid: str) -> dict:
        b, source = self.building(bid)
        return building_json(b, source)

    # ----------------------------------------------------------------- runs ---
    def _run(self, sid: str):
        if sid not in self.sims:
            raise KeyError("This simulation is no longer in memory - run it again.")
        return self.sims[sid]

    def store_sim(self, bid: str, r) -> str:
        """Keep a finished run; its playback is packed to 16-bit positions (r.packed) and the float copy dropped."""
        sid = uuid.uuid4().hex[:8]
        b, _ = self.buildings[bid]
        if r.frames is not None:
            r.packed = pack_frames(r.frames, b.origin)
            r.origin = b.origin
            r.frames = None
        with self.lock:
            self.sims[sid] = (bid, r)
            while len(self.sims) > MAX_SIMS:
                self.sims.pop(next(iter(self.sims)))
        return sid

    def find_baseline(self, bid: str, params: dict):
        """Most recent run on the same building with the same people/settings and nothing closed."""
        with self.lock:
            items = list(self.sims.items())
        for sid, (b_id, r) in reversed(items):
            p = r.params
            if b_id == bid and not (p["closed_exits"] or p["closed_doors"] or p["closed_stairs"]) \
                    and _same_people(p, params):
                return sid, r
        return None, None

    def sim(self, sid: str) -> dict:
        bid, r = self._run(sid)
        q, step = r.packed
        out = {
            "sim_id": sid, "building_id": bid, "stats": r.stats, "params": r.params, "frames": int(q.shape[0]),
            "people": int(q.shape[1]) if q.ndim == 3 else 0, "frame_step": step,
            "frame_dt": float(r.frame_times[1] - r.frame_times[0]) if len(r.frame_times) > 1 else 0.2,
            "node_max_density": b64(r.node_max_density.astype(np.float32)),
            "node_congested_s": b64(r.node_congested_s.astype(np.float32)),
            "exit_of": b64(r.exit_of.astype(np.int16)),
            "status": b64(r.status.astype(np.int8)),
            "closed_nodes": b64(np.nonzero(r.closed)[0].astype(np.int32)),
            "compare": None,
        }
        if r.stats["scenario"]["closed"]:
            base_id, base = self.find_baseline(bid, r.params)
            if base is not None:
                out["compare"] = {"baseline_sim": base_id, **compare(base, r), "baseline": summary(base),
                                  "baseline_curve": base.stats["curve"]}
        return out

    def frames(self, sid: str) -> bytes:
        """Playback as little-endian uint16 x, z, h relative to the building origin; multiply by the run's
        frame_step (m). 65535 = person not shown."""
        _, r = self._run(sid)
        return r.packed[0].astype("<u2").tobytes()

    def export_results(self, sid: str, paths: bool = False) -> dict:
        _, r = self._run(sid)
        frames = unpack_frames(*r.packed, r.origin) if paths else None
        return results_json(r, include_paths=paths, frames=frames)

    # ---------------------------------------------------------- long jobs ---
    # Each returns work(report) after checking the request, so bad input fails before any job starts.
    def simulate_job(self, body: dict):
        """One scenario. If something is closed and there is no matching all-open run yet, that runs too
        (in parallel where the platform allows) so the result can be compared."""
        bid = body["id"]
        b, _ = self.building(bid)
        p = SimParams.from_dict(body)
        closures(b, p)  # reject unknown ids before starting

        def work(report):
            if p.has_closures() and self.find_baseline(bid, asdict(p))[1] is None:
                report("Simulating this scenario and, to compare, the same people with nothing closed…")
                base, r = run_many(b, [p.without_closures(), p])
                self.store_sim(bid, base)
            else:
                r = simulate(b, p, progress=lambda t, n, P: report(f"{t:.0f} s simulated · {n} of {P} people out"))
            return self.sim(self.store_sim(bid, r))
        return work

    def stress_job(self, body: dict):
        """Close each main exit / staircase on its own and rank the results."""
        bid = body["id"]
        b, _ = self.building(bid)
        p = SimParams.from_dict(body).without_closures()

        def work(report):
            base_id, base = self.find_baseline(bid, asdict(p))
            baseline, rows = stress_test(b, p, baseline=base, progress=report)
            if base_id is None:
                base_id = self.store_sim(bid, baseline)
            out = [{**row, "sim_id": self.store_sim(bid, r)} for row, r in rows]
            return {"baseline_sim": base_id, "baseline": summary(baseline), "awareness": p.awareness, "rows": out}
        return work
