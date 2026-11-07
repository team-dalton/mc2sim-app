"""Serialise building models and simulation results.

building.json is the hand-off format for other simulators (see docs/building_format.md).
render_payload() packs geometry compactly (base64 typed arrays) for the web viewer.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone

import numpy as np

from .analyze import KIND_NAMES, Building
from .blocks import DOOR, PASS, SHAPE_NAMES
from .sim import SimResult

FORMAT_VERSION = 1


def _strip(items):
    return [{k: v for k, v in it.items() if k != "nodes"} for it in items]


def _stair_ids(b: Building) -> np.ndarray:
    return b.stair_id if b.stair_id is not None else np.full(b.n, -1, np.int64)


def building_json(b: Building, source: dict | None = None) -> dict:
    ox, oy, oz = b.origin
    return {
        "format": "mc2sim.building",
        "version": FORMAT_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source or {},
        "units": {"length": "m", "note": "Minecraft world coordinates, y is up, 1 block = 1 m. "
                                           "A walkable cell (x, z) covers [x, x+1] x [z, z+1]."},
        "summary": b.summary(),
        "levels": b.levels,
        "rooms": b.rooms,
        "doors": _strip(b.doors),
        "exits": _strip(b.exits),
        "stairs": _strip(b.stairs),
        "objects": b.objects,
        "signs": b.signs,
        "corrections": b.fixes,
        "walkable": {
            "x": (b.nx + ox).tolist(),
            "z": (b.nz + oz).tolist(),
            "h": b.h.round(2).tolist(),
            "kind": [KIND_NAMES[k] for k in b.kind.tolist()],
            "kind_codes": KIND_NAMES,
            "level": b.level.tolist(),
            "room": b.room.tolist(),
            "door": b.door_id.tolist(),
            "exit": b.exit_id.tolist(),
            "stairs": _stair_ids(b).tolist(),
            "indoor": b.indoor.astype(int).tolist(),
            "safe": b.safe.astype(int).tolist(),
            "uphill": b.uphill.tolist(),
            "neighbors": b.nbr.tolist(),
            "neighbor_order": ["+x (east)", "-x (west)", "+z (south)", "-z (north)"],
        },
    }


def results_json(r: SimResult, include_paths: bool = False, frames: np.ndarray | None = None) -> dict:
    """`frames` (world x, z, h) overrides r.frames, e.g. when the server keeps them packed."""
    out = {"format": "mc2sim.results", "version": FORMAT_VERSION, "params": r.params, "stats": r.stats}
    if r.status is not None:
        out["person_status"] = r.status.tolist()
        out["person_status_codes"] = ["out", "no way out", "still inside at end", "out, not at the assembly point"]
    if include_paths:
        fr = r.frames if frames is None else frames
        out["frame_times"] = r.frame_times.round(3).tolist()
        out["frames_xzh"] = np.where(np.isnan(fr), None, fr.round(2)).tolist()
    return out


# playback frames are kept and sent as 16-bit positions relative to the building origin
FRAME_NONE = 65535


def pack_frames(frames: np.ndarray, origin) -> tuple[np.ndarray, float]:
    """World x, z, h frames [F, P, 3] (NaN = not shown) -> (uint16 array, step in m). The step is 1 cm for
    anything up to 655 m across, coarser only for bigger areas; it halves the memory of float32."""
    off = np.array([origin[0], origin[2], origin[1]], np.float32)
    rel = frames - off
    ok = np.isfinite(rel)
    top = float(rel[ok].max()) if ok.any() else 1.0
    step = max(0.01, float(np.ceil(max(top, 1.0) * 100 / (FRAME_NONE - 1))) / 100)
    q = np.full(rel.shape, FRAME_NONE, np.uint16)
    q[ok] = np.clip(np.round(rel[ok] / step), 0, FRAME_NONE - 1).astype(np.uint16)
    return q, step


def unpack_frames(q: np.ndarray, step: float, origin) -> np.ndarray:
    out = q.astype(np.float32) * np.float32(step) + np.array([origin[0], origin[2], origin[1]], np.float32)
    out[q == FRAME_NONE] = np.nan
    return out


def b64(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")


def render_payload(b: Building) -> dict:
    """Visible blocks + walkable cells for the 3D viewer. Coordinates relative to b.origin."""
    shp = b.shape
    X, Y, Z = shp.shape
    open_ = (shp == PASS) | (shp == DOOR)
    solid = ~((shp == PASS))
    solid[:, 0, :] = False  # sealed bottom layer is not drawn
    exposed = np.zeros_like(solid)
    pad = np.pad(open_, 1, constant_values=True)
    for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
        exposed |= pad[1 + dx:X + 1 + dx, 1 + dy:Y + 1 + dy, 1 + dz:Z + 1 + dz]
    exposed &= solid
    xs, ys, zs = np.nonzero(exposed)
    pid = b.ids[xs, ys, zs]
    used, inv = np.unique(pid, return_inverse=True)
    shape_at = shp[xs, ys, zs]
    palette = []
    for i, u in enumerate(used.tolist()):
        palette.append({"state": b.states[u].split("[")[0].replace("minecraft:", ""),
                        "rgb": [int(c) for c in b.rgb[u]], "role": b.roles[u],
                        "shape": SHAPE_NAMES[int(np.bincount(shape_at[inv == i]).argmax())]})
    blocks = np.column_stack([xs, ys, zs]).astype(np.int16)
    nodes = np.column_stack([b.nx + 0.5, b.h - b.origin[1], b.nz + 0.5]).astype(np.float32)
    flags = (b.safe.astype(np.uint8) | (b.indoor.astype(np.uint8) << 1) | ((b.exit_id >= 0).astype(np.uint8) << 2))
    return {
        "origin": list(b.origin), "size": [X, Y, Z],
        "blocks": b64(blocks), "block_pal": b64(inv.astype(np.uint16)), "palette": palette,
        "nodes": b64(nodes), "node_kind": b64(b.kind.astype(np.uint8)), "node_flags": b64(flags),
        "node_room": b64(b.room.astype(np.int32)), "node_level": b64(b.level.astype(np.int16)),
        "node_exit": b64(b.exit_id.astype(np.int16)), "node_door": b64(b.door_id.astype(np.int16)),
        "node_stair": b64(_stair_ids(b).astype(np.int16)),
        "node_count": int(b.n), "block_count": int(len(xs)),
        "plan": plan_payload(b),
    }


def plan_payload(b: Building) -> dict:
    """Per-floor 2D plans: what is at body height (feet block, else head block) in every column.
    `shape` uses blocks.SHAPE_NAMES codes (+8 when it is the head block), `name` indexes `names`
    (block id without namespace/properties).
    Arrays are [x, z] in C order (index = x * Z + z), relative to the building origin."""
    shp = b.shape
    X, Y, Z = shp.shape
    oy = b.origin[1]
    base = [s.split("[")[0].replace("minecraft:", "") for s in b.states]
    names = sorted(set(base))
    index = {n: i for i, n in enumerate(names)}
    name_of_state = np.array([index[n] for n in base], np.uint16)
    role_of_name: dict[str, str] = {}
    for s, n in enumerate(base):
        role_of_name.setdefault(n, b.roles[s])
    levels = []
    for L, lv in enumerate(b.levels):
        fy = min(max(int(np.ceil(lv["elevation"])) - oy, 0), Y - 1)
        hy = min(fy + 1, Y - 1)
        feet, head = shp[:, fy, :], shp[:, hy, :]
        use_feet = feet != PASS
        code = np.where(use_feet, feet, head + 8).astype(np.uint8)   # +8: the head block was used
        sid = np.where(use_feet, b.ids[:, fy, :], b.ids[:, hy, :])
        levels.append({"level": L, "feet_y": int(fy + oy), "shape": b64(code), "name": b64(name_of_state[sid])})
    return {"size": [X, Z], "names": names, "roles": [role_of_name[n] for n in names], "levels": levels}
