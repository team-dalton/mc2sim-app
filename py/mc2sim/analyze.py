"""Turn a block Volume into a building model: walkable surface, stairs, doors, exits, floors, rooms, furniture.

Works on any world (no markers needed) using geometric heuristics. New worlds can add
signs to override or enrich the result:
  [EXIT]          this spot is an exit / safe place
  [ROOM Name]     name the room the sign is in
  [PEOPLE 30]     put 30 people in this room for simulations
  [LOCKED]        the door next to this sign cannot be used

Corrections made in the app's floor plan (`fixes`) override the heuristics, see FIX_KINDS.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from .blocks import BARRIER, DOOR, LIQUID, OBST, PASS, SLAB, SOLID, STAIRS, classify_palette, parse_state
from .world import Volume

DIRS = np.array([(1, 0), (-1, 0), (0, 1), (0, -1)], np.int64)  # E, W, S, N  (dx, dz)
OPP = np.array([1, 0, 3, 2])
FLOOR, STAIR, DOORWAY = 0, 1, 2
KIND_NAMES = ["floor", "stairs", "door"]
# user corrections: block fixes act on the block at (x, y, z); cell fixes on the walkable cell standing at height y
BLOCK_FIXES = {"chair", "table", "stairs", "obstacle", "open"}
CELL_FIXES = {"exit", "not_exit"}
FIX_KINDS = {
    "chair": "this block is a chair (people sit in front of it)",
    "table": "this block is a table",
    "stairs": "this stair block is part of a staircase, not a chair",
    "obstacle": "this block is just an obstacle (no furniture)",
    "open": "people can walk through this block",
    "exit": "this walkable cell is a real exit / safe place",
    "not_exit": "these cells are not an exit (people cannot get out here)",
    "levels": "use these floor heights instead of the detected ones",
}


@dataclass
class Building:
    origin: tuple[int, int, int]          # world coords of grid index (0,0,0)
    states: list[str]
    ids: np.ndarray                        # [X,Y,Z] palette ids (cropped)
    shape: np.ndarray                      # [X,Y,Z] shape codes after furniture detection
    roles: list[str]
    rgb: np.ndarray
    # nodes (walkable 1x1 m cells)
    nx: np.ndarray
    ny: np.ndarray                         # support block y index
    nz: np.ndarray
    h: np.ndarray                          # standing elevation, world y (m)
    kind: np.ndarray
    nbr: np.ndarray                        # [N,4] neighbour node ids (E,W,S,N) or -1
    uphill: np.ndarray                     # [N,2] stair climbing direction (dx,dz)
    indoor: np.ndarray
    safe: np.ndarray
    level: np.ndarray
    room: np.ndarray
    exit_id: np.ndarray
    door_id: np.ndarray
    seat_nodes: np.ndarray                 # node ids where seated people stand up
    levels: list = field(default_factory=list)
    rooms: list = field(default_factory=list)
    exits: list = field(default_factory=list)
    doors: list = field(default_factory=list)
    objects: list = field(default_factory=list)
    signs: list = field(default_factory=list)
    room_people: dict = field(default_factory=dict)
    ground_h: float | None = None
    warnings: list = field(default_factory=list)
    stairs: list = field(default_factory=list)   # staircases that connect floors (rise >= 2 m)
    stair_id: np.ndarray | None = None           # per node: staircase id or -1
    fixes: list = field(default_factory=list)    # user corrections that were applied
    standing_heights: list = field(default_factory=list)  # [[y, m2], ...] common indoor standing heights
    detected_levels: list = field(default_factory=list)   # floor heights the detection found (before corrections)

    @property
    def n(self) -> int:
        return len(self.nx)

    def world_xz(self, nodes):
        return self.nx[nodes] + self.origin[0], self.nz[nodes] + self.origin[2]

    def summary(self) -> dict:
        return {
            "origin": list(self.origin),
            "size": list(self.ids.shape),
            "walkable_m2": int(self.n),
            "indoor_m2": int(self.indoor.sum()),
            "stair_cells": int((self.kind == STAIR).sum()),
            "levels": self.levels,
            "rooms": len(self.rooms),
            "doors": len(self.doors),
            "exits": len(self.exits),
            "stairs": len(self.stairs),
            "seats": int(len(self.seat_nodes)),
            "objects": _count_types(self.objects),
            "ground_h": self.ground_h,
            "standing_heights": self.standing_heights,
            "detected_levels": self.detected_levels,
            "warnings": self.warnings,
        }


def _count_types(objs):
    out: dict[str, int] = {}
    for o in objs:
        out[o["type"]] = out.get(o["type"], 0) + 1
    return out


# --------------------------------------------------------------- entry point ---
def analyze(vol: Volume, ymin: int | None = None, ymax: int | None = None, fixes: list | None = None) -> Building:
    """`fixes`: user corrections [{kind, x, y, z}] in world coordinates (kind in FIX_KINDS); a not_exit fix
    may list several cells as `cells: [[x, y, z], ...]`."""
    shape_lut, roles, rgb, facing_lut, stair_shape = classify_palette(vol.states)
    ids = vol.ids
    shp = shape_lut[ids]
    ox, oy, oz = vol.origin
    warnings: list[str] = []

    # ---- crop vertically: ground level (auto) .. top of tallest structure
    nonpass = (shp != PASS)
    colmax = np.where(nonpass.any(axis=1), ids.shape[1] - 1 - np.argmax(nonpass[:, ::-1, :], axis=1), -1)
    X, Y, Z = ids.shape
    perim = np.zeros((X, Z), bool)
    perim[0, :] = perim[-1, :] = perim[:, 0] = perim[:, -1] = True
    vals = colmax[perim & (colmax >= 0)]
    if ymin is None:
        ground_top = int(np.bincount(vals).argmax()) if len(vals) else 0
        lo = max(0, ground_top - 1)
    else:
        lo = max(0, int(ymin) - oy)
    hi = int(colmax.max()) + 4 if ymax is None else int(ymax) - oy
    hi = min(max(hi, lo + 3), Y - 1)
    ids = ids[:, lo:hi + 1, :].copy()
    shp = shp[:, lo:hi + 1, :].copy()
    shp[:, 0, :] = np.where(shp[:, 0, :] == PASS, SOLID, shp[:, 0, :])  # seal the bottom
    oy = oy + lo
    X, Y, Z = ids.shape

    # ---- user corrections
    fixes = [dict(f) for f in (fixes or [])]
    fix_block, fix_cells = _parse_fixes(fixes, (ox, oy, oz), (X, Y, Z), warnings)
    orig_stair = shp == STAIRS
    for (x, y, z), k in fix_block.items():
        if k == "open":
            shp[x, y, z] = PASS
        elif k == "obstacle" or (k == "table" and shp[x, y, z] == PASS):
            shp[x, y, z] = OBST
        elif k == "stairs" and not orig_stair[x, y, z]:
            warnings.append(f"Correction 'stairs' at {x + ox},{y + oy},{z + oz} is not on a stair block - ignored")

    is_stair = shp == STAIRS
    fac = facing_lut[ids]  # [X,Y,Z,2]
    passable = (shp == PASS) | (shp == DOOR)

    def P(a, x, y, z, default=False):
        """Safe gather from a 3D array with out-of-bounds default."""
        ok = (x >= 0) & (x < X) & (y >= 0) & (y < Y) & (z >= 0) & (z < Z)
        out = np.full(x.shape, default, dtype=a.dtype)
        out[ok] = a[x[ok], y[ok], z[ok]]
        return out

    # ---- furniture: stair chairs (stairs that are not part of a staircase)
    objects: list[dict] = []
    sx, sy, sz = np.nonzero(is_stair)
    fx, fz = fac[sx, sy, sz, 0].astype(np.int64), fac[sx, sy, sz, 1].astype(np.int64)
    below_ok = ~P(passable, sx, sy - 1, sz, True) & (P(shp, sx, sy - 1, sz, PASS) != LIQUID)
    head_ok = P(passable, sx, sy + 1, sz, True)
    bx_, bz_ = sx + fx, sz + fz          # behind (uphill)
    step_up = ((P(shp, bx_, sy, bz_, PASS) == SOLID) & P(passable, bx_, sy + 1, bz_, True)) | \
              (P(shp, bx_, sy + 1, bz_, PASS) == STAIRS) | \
              ((P(shp, bx_, sy + 1, bz_, PASS) == SLAB) & P(passable, bx_, sy + 2, bz_, True))
    # part of a flight (or a sloped roof): another stair diagonally one block up or down
    in_run = np.zeros_like(step_up)
    for k in range(4):
        dx, dz = DIRS[k]
        for dy in (-1, 1):
            in_run |= P(shp, sx + dx, sy + dy, sz + dz, PASS) == STAIRS
    chair = below_ok & head_ok & ~step_up & ~in_run
    for (x, y, z), k in fix_block.items():
        if k in ("chair", "stairs") and is_stair[x, y, z]:
            chair[np.nonzero((sx == x) & (sy == y) & (sz == z))[0]] = k == "chair"
    for i in np.nonzero(chair)[0]:
        x, y, z = int(sx[i]), int(sy[i]), int(sz[i])
        shp[x, y, z] = OBST
        objects.append({"type": "chair", "x": x + ox, "y": y + oy, "z": z + oz,
                        "facing": [int(-fx[i]), int(-fz[i])]})
    for (x, y, z), k in fix_block.items():   # chairs marked on blocks that are not stairs
        if k == "chair" and not orig_stair[x, y, z]:
            shp[x, y, z] = OBST
            objects.append({"type": "chair", "x": x + ox, "y": y + oy, "z": z + oz, "facing": [0, 0]})
    is_stair = shp == STAIRS
    passable = (shp == PASS) | (shp == DOOR)

    # ---- walkable nodes
    support = (shp == SOLID) | (shp == SLAB) | (shp == STAIRS)
    clear1 = np.zeros_like(passable)
    clear1[:, :-1, :] = passable[:, 1:, :]
    clear2 = np.ones_like(passable)
    clear2[:, :-2, :] = passable[:, 2:, :]
    walk = support & clear1 & clear2
    nx, ny, nz = (a.astype(np.int64) for a in np.nonzero(walk))
    N = len(nx)
    if N == 0:
        raise ValueError("No walkable surface found in the selection")
    node_at = np.full((X, Y, Z), -1, np.int64)
    node_at[nx, ny, nz] = np.arange(N)
    sup = shp[nx, ny, nz]
    h = (ny + oy).astype(np.float64) + np.where(sup == SOLID, 1.0, 0.5)
    kind = np.where(sup == STAIRS, STAIR, FLOOR).astype(np.uint8)
    feet = np.minimum(ny + 1, Y - 1)
    kind[(shp[nx, feet, nz] == DOOR) & (kind == FLOOR)] = DOORWAY
    up1 = np.zeros((N, 2), np.int64)
    up2 = np.zeros((N, 2), np.int64)
    st = kind == STAIR
    up1[st] = fac[nx[st], ny[st], nz[st]].astype(np.int64)
    for i in np.nonzero(st)[0]:
        s = stair_shape[ids[nx[i], ny[i], nz[i]]]
        dx, dz = up1[i]
        if s.endswith("left"):
            up2[i] = (dz, -dx)
        elif s.endswith("right"):
            up2[i] = (-dz, dx)

    # ---- neighbour graph
    nbr = np.full((N, 4), -1, np.int64)
    for k in range(4):
        dx, dz = DIRS[k]
        best = np.full(N, -1, np.int64)
        for o in (0, -1, 1):
            c = P(node_at, nx + dx, ny + o, nz + dz, -1)
            ok = (c >= 0) & (best < 0)
            if not ok.any():
                continue
            idx = np.nonzero(ok)[0]
            cc = c[idx]
            dh = h[cc] - h[idx]
            allowed = np.abs(dh) <= 0.51
            big = (np.abs(dh) > 0.51) & (np.abs(dh) <= 1.01)
            if big.any():
                lower = np.where(dh > 0, idx, cc)
                upper = np.where(dh > 0, cc, idx)
                sgn = np.where(dh > 0, 1, -1)
                ddx, ddz = dx * sgn, dz * sgn  # direction from lower to upper
                def climbs(n):
                    return (kind[n] == STAIR) & (((up1[n, 0] == ddx) & (up1[n, 1] == ddz)) |
                                                 ((up2[n, 0] == ddx) & (up2[n, 1] == ddz)))
                allowed |= big & (climbs(lower) | climbs(upper))
            best[idx[allowed]] = cc[allowed]
        nbr[:, k] = best
    # make symmetric (keep only mutual links)
    for k in range(4):
        has = nbr[:, k] >= 0
        back = np.full(N, -1, np.int64)
        back[has] = nbr[nbr[has, k], OPP[k]]
        nbr[has & (back != np.arange(N)), k] = -1

    # ---- indoor (roofed) vs outdoor
    leaves = np.array([r == "leaves" for r in roles])
    roof = ~passable & ~leaves[ids] & (shp != LIQUID)
    top_roof = np.where(roof.any(axis=1), Y - 1 - np.argmax(roof[:, ::-1, :], axis=1), -1)
    indoor = top_roof[nx, nz] > ny + 2

    b = Building(origin=(ox, oy, oz), states=vol.states, ids=ids, shape=shp, roles=roles, rgb=rgb,
                 nx=nx, ny=ny, nz=nz, h=h, kind=kind, nbr=nbr, uphill=up1, indoor=indoor,
                 safe=np.zeros(N, bool), level=np.full(N, -1, np.int64), room=np.full(N, -1, np.int64),
                 exit_id=np.full(N, -1, np.int64), door_id=np.full(N, -1, np.int64),
                 seat_nodes=np.zeros(0, np.int64), objects=objects, warnings=warnings, fixes=fixes)

    _find_safe_area(b)
    _apply_signs(b, vol.signs)
    _apply_cell_fixes(b, fix_cells)
    _find_exits(b)
    _find_doors(b)
    _find_levels(b, next((f for f in fixes if f.get("kind") == "levels"), None))
    _find_stairs(b)
    _find_rooms(b)
    _find_objects(b, fix_block)
    _seat_nodes(b)
    _assign_sign_rooms(b)
    return b


# ------------------------------------------------------------------ helpers ---
def _parse_fixes(fixes, origin, size, warnings):
    """Split corrections into {grid (x,y,z): kind} for blocks and [(kind, gx, h, gz)] for walkable cells."""
    ox, oy, oz = origin
    X, Y, Z = size
    blocks: dict[tuple[int, int, int], str] = {}
    cells: list[tuple[str, int, float, int]] = []
    for f in fixes:
        k = f.get("kind")
        if k == "levels":       # floor heights: handled by _find_levels
            continue
        pts = f.get("cells") or [[f.get("x"), f.get("y"), f.get("z")]]
        if k not in BLOCK_FIXES and k not in CELL_FIXES:
            warnings.append(f"Unknown correction '{k}' - ignored")
            continue
        for p in pts:
            try:
                x, y, z = int(np.floor(float(p[0]))), float(p[1]), int(np.floor(float(p[2])))
            except (TypeError, ValueError, IndexError):
                warnings.append(f"Correction '{k}' has no valid position - ignored")
                continue
            if k in CELL_FIXES:
                cells.append((k, x - ox, y, z - oz))
                continue
            g = (x - ox, int(round(y)) - oy, z - oz)
            if 0 <= g[0] < X and 0 <= g[1] < Y and 0 <= g[2] < Z:
                blocks[g] = k
            else:
                warnings.append(f"Correction '{k}' at {x},{int(round(y))},{z} is outside the building model - ignored")
    return blocks, cells


def _apply_cell_fixes(b: Building, cells):
    for k, x, y, z in cells:
        cand = np.nonzero((b.nx == x) & (b.nz == z))[0]
        n = cand[np.argmin(np.abs(b.h[cand] - y))] if len(cand) else -1
        if n < 0 or abs(b.h[n] - y) > 1.6:
            b.warnings.append(f"Correction '{k}' at {x + b.origin[0]},{y},{z + b.origin[2]} is not on a walkable cell - ignored")
            continue
        if k == "exit":
            b.safe[n] = True
        else:
            _remove_node(b, n)


def _components(b: Building, mask: np.ndarray):
    """Connected components of nodes in `mask` using the walk graph. Returns labels (-1 outside mask)."""
    idx = np.nonzero(mask)[0]
    lab = np.full(b.n, -1, np.int64)
    if len(idx) == 0:
        return lab, 0
    local = np.full(b.n, -1, np.int64)
    local[idx] = np.arange(len(idx))
    rows, cols = [], []
    for k in range(4):
        nb = b.nbr[idx, k]
        ok = nb >= 0
        ok[ok] &= mask[nb[ok]]
        rows.append(local[idx[ok]])
        cols.append(local[nb[ok]])
    r = np.concatenate(rows)
    c = np.concatenate(cols)
    g = coo_matrix((np.ones(len(r)), (r, c)), shape=(len(idx), len(idx))).tocsr()
    ncomp, labels = connected_components(g, directed=False)
    lab[idx] = labels
    return lab, ncomp


def _find_safe_area(b: Building):
    X, _, Z = b.ids.shape
    outdoor = ~b.indoor
    perim = (b.nx == 0) | (b.nx == X - 1) | (b.nz == 0) | (b.nz == Z - 1)
    cand = outdoor & perim
    if not cand.any():
        b.warnings.append("No outdoor ground at the selection edge - enlarge the selection so it includes "
                          "some ground around the building.")
        cand = outdoor
    if not cand.any():
        b.warnings.append("No outdoor area at all - evacuation targets cannot be found.")
        return
    hv = np.round(b.h[cand] * 2).astype(np.int64)
    ground = np.bincount(hv - hv.min()).argmax() + hv.min()
    b.ground_h = float(ground / 2)
    lab, _ = _components(b, outdoor)
    touching = np.unique(lab[cand & (lab >= 0)])
    b.safe = outdoor & np.isin(lab, touching) & (np.abs(b.h - b.ground_h) <= 3.0)


def _sign_nodes(b: Building, s: dict, radius=3):
    x, y, z = s["x"] - b.origin[0], s["y"] - b.origin[1], s["z"] - b.origin[2]
    d = np.abs(b.nx - x) + np.abs(b.nz - z) + 0.5 * np.abs(b.ny + 1 - y)
    order = np.argsort(d)
    return [int(i) for i in order[:6] if d[i] <= radius]


def _apply_signs(b: Building, signs: list[dict]):
    tag = re.compile(r"\[\s*(EXIT|ROOM|PEOPLE|LOCKED)\s*([^\]]*)\]", re.I)
    for s in signs:
        for m in tag.finditer(s["text"]):
            key, arg = m.group(1).upper(), m.group(2).strip()
            near = _sign_nodes(b, s)
            entry = {"tag": key, "arg": arg, "x": s["x"], "y": s["y"], "z": s["z"], "node": near[0] if near else None}
            b.signs.append(entry)
            if not near:
                b.warnings.append(f"Sign [{key}] at {s['x']},{s['y']},{s['z']} is not next to a walkable spot")
                continue
            if key == "EXIT":
                b.safe[near[0]] = True
            elif key == "LOCKED":
                for n in near[:3]:
                    if b.kind[n] == DOORWAY:
                        _remove_node(b, n)


def _remove_node(b: Building, n: int):
    for k in range(4):
        m = b.nbr[n, k]
        if m >= 0:
            b.nbr[m, OPP[k]] = -1
            b.nbr[n, k] = -1


def _find_exits(b: Building):
    thr = np.zeros(b.n, bool)
    for k in range(4):
        nb = b.nbr[:, k]
        ok = nb >= 0
        thr[ok] |= b.safe[nb[ok]]
    thr &= ~b.safe
    lab, n = _components(b, thr)
    b.exit_id = lab
    b.exits = []
    for e in range(n):
        nodes = np.nonzero(lab == e)[0]
        x, z = b.world_xz(nodes)
        b.exits.append({"id": e, "name": f"Exit {e + 1}", "width_m": int(len(nodes)),
                        "x": float(x.mean() + 0.5), "z": float(z.mean() + 0.5), "y": float(b.h[nodes].mean()),
                        "is_door": bool((b.kind[nodes] == DOORWAY).any()), "nodes": nodes.tolist()})


def _find_doors(b: Building):
    lab, n = _components(b, b.kind == DOORWAY)
    b.door_id = lab
    b.doors = []
    for d in range(n):
        nodes = np.nonzero(lab == d)[0]
        x, z = b.world_xz(nodes)
        pid = b.ids[b.nx[nodes[0]], min(b.ny[nodes[0]] + 1, b.ids.shape[1] - 1), b.nz[nodes[0]]]
        name = parse_state(b.states[pid])[0].split(":")[-1]
        b.doors.append({"id": d, "name": f"Door {d + 1}", "width_m": int(len(nodes)), "x": float(x.mean() + 0.5), "z": float(z.mean() + 0.5),
                        "y": float(b.h[nodes].mean()), "block": name,
                        "access": "controlled" if name == "iron_door" else "free",
                        "is_exit": bool((b.exit_id[nodes] >= 0).any()), "nodes": nodes.tolist()})


def _measured_height(b: Building, cells: np.ndarray) -> float | None:
    """Storey height in blocks measured from the ceiling: from where people stand on these cells to the top of
    the first block above their heads (where the floor above would stand). Median over the cells, 0.5 steps;
    None when most of them are open to the sky."""
    if len(cells) < 4:
        return None
    oy = b.origin[1]
    Y = b.shape.shape[1]
    cols = b.shape[b.nx[cells], :, b.nz[cells]]                       # [n, Y]
    y0 = np.ceil(b.h[cells] - oy - 1e-6).astype(np.int64)          # first block fully above the standing surface
    above = np.arange(Y)[None, :] >= y0[:, None]
    roof = above & (cols != PASS) & (cols != DOOR)
    has = roof.any(axis=1)
    if has.sum() < max(4, 0.5 * len(cells)):
        return None
    top = roof[has].argmax(axis=1) + oy + 1                          # top of the first block overhead, world y
    return float(np.round(np.median(top - b.h[cells][has]) * 2) / 2)


def _find_levels(b: Building, override: dict | None = None):
    """Floors = the most common indoor standing heights (>= 2 m apart). A 'levels' correction
    ({"kind": "levels", "levels": [{"elevation": y, "name": "1F", "height": 5}, ...]}) replaces the detection;
    elevation is the y you stand at on that floor (what Minecraft's F3 screen shows).

    Each floor's `height` (blocks it occupies) is the gap to the floor above; for the top floor it is the
    correction's height if given, else measured from its ceiling, else the usual gap between floors."""
    m = b.indoor & (b.kind != STAIR)
    chosen: list[float] = []
    names: dict[float, str] = {}
    given_h: dict[float, float] = {}
    if m.any():
        hv = np.round(b.h[m] * 2) / 2
        uniq, cnt = np.unique(hv, return_counts=True)
        # common standing heights, offered in the app as suggestions for floor heights
        common = sorted(((float(u), int(c)) for u, c in zip(uniq, cnt) if c >= 15), key=lambda t: -t[1])[:12]
        b.standing_heights = [[u, c] for u, c in sorted(common)]
        thresh = max(15, 0.02 * m.sum())
        peaks = sorted(zip(uniq[cnt >= thresh], cnt[cnt >= thresh]), key=lambda t: -t[1])
        for hgt, _ in peaks:
            if all(abs(hgt - c) >= 2.0 for c in chosen):
                chosen.append(float(hgt))
        chosen.sort()
        if not chosen:
            chosen = [float(np.median(hv))]
    b.detected_levels = list(chosen)
    if override is not None:
        given = []
        for lv in override.get("levels") or []:
            try:
                e = round(float(lv["elevation"]) * 2) / 2
            except (KeyError, TypeError, ValueError):
                b.warnings.append("A floor in the floor-height correction has no valid height - ignored")
                continue
            if any(abs(e - g_) < 1.0 for g_ in given):
                b.warnings.append(f"Two floors at almost the same height (y {e}) - kept one")
                continue
            given.append(e)
            if str(lv.get("name") or "").strip():
                names[e] = str(lv["name"]).strip()[:24]
            try:
                hgt = round(float(lv["height"]) * 2) / 2
                if 1 <= hgt <= 100:
                    given_h[e] = hgt
            except (KeyError, TypeError, ValueError):
                pass
        if given:
            chosen = sorted(given)
        else:
            b.warnings.append("The floor-height correction lists no floors - using the detected floors")
    if not chosen:
        b.levels = []
        return
    lv = np.array(chosen)
    d = np.abs(b.h[:, None] - lv[None, :])
    near = d.argmin(axis=1)
    b.level = np.where(d.min(axis=1) <= 1.5, near, -1)
    # ground floor = level closest to ground height
    g = int(np.abs(lv - (b.ground_h if b.ground_h is not None else lv[0])).argmin())
    measured = [_measured_height(b, np.nonzero((b.level == i) & m)[0]) for i in range(len(chosen))]
    gaps = np.diff(lv)
    top = given_h.get(chosen[-1]) or measured[-1] or (float(np.median(gaps)) if len(gaps) else 4.0)
    heights = [float(x) for x in gaps] + [float(top)]
    b.levels = [{"id": i, "name": names.get(e) or _floor_name(i - g), "elevation": float(e), "height": heights[i],
                 "measured_height": measured[i], "indoor_m2": int(((b.level == i) & b.indoor).sum())}
                for i, e in enumerate(chosen)]


def _find_stairs(b: Building):
    """Staircases = the cells that climb from one floor to another.

    A flight is a connected set of stair cells and indoor cells between floor heights (this also
    catches staircases built from slabs or full blocks) that touches two or more floors. Flights stacked
    at the same spot form one staircase, so closing it models a whole stair core being unusable."""
    b.stair_id = np.full(b.n, -1, np.int64)
    b.stairs = []
    if len(b.levels) < 2:
        return
    lab, n = _components(b, (b.kind == STAIR) | (b.indoor & (b.level < 0)))
    flights = []
    for c in range(n):
        comp = np.nonzero(lab == c)[0]
        nb = b.nbr[comp].ravel()
        nb = nb[nb >= 0]
        nb = nb[lab[nb] != c]
        lv = {int(v) for v in b.level[nb] if v >= 0}
        if len(lv) >= 2:
            flights.append((comp, lv, (b.nx[comp].min(), b.nz[comp].min(), b.nx[comp].max(), b.nz[comp].max())))
    # stack flights whose footprints overlap (union-find)
    parent = list(range(len(flights)))
    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i, (_, _, a) in enumerate(flights):
        for j in range(i):
            c = flights[j][2]
            if a[0] <= c[2] + 1 and c[0] <= a[2] + 1 and a[1] <= c[3] + 1 and c[1] <= a[3] + 1:
                parent[root(i)] = root(j)
    groups: dict[int, list] = {}
    for i in range(len(flights)):
        groups.setdefault(root(i), []).append(i)
    cores = []
    for members in groups.values():
        nodes = np.unique(np.concatenate([flights[i][0] for i in members]))
        lv = sorted(set().union(*(flights[i][1] for i in members)))
        cores.append((nodes, lv))
    cores.sort(key=lambda t: (float(b.nx[t[0]].mean()), float(b.nz[t[0]].mean())))
    for k, (nodes, lv) in enumerate(cores):
        names = [b.levels[v]["name"] for v in lv]
        x, z = b.world_xz(nodes)
        b.stair_id[nodes] = k
        b.stairs.append({"id": k, "name": f"Stairs {k + 1}", "levels": names,
                         "label": f"Stairs {k + 1} ({names[0]}–{names[-1]})", "cells": int(len(nodes)),
                         "x": float(x.mean() + 0.5), "z": float(z.mean() + 0.5), "y": float(b.h[nodes].min()),
                         "indoor": bool(b.indoor[nodes].mean() >= 0.5), "nodes": nodes.tolist()})


def _floor_name(rel: int) -> str:
    if rel < 0:
        return f"B{-rel}"
    return f"{rel + 1}F"


def _find_rooms(b: Building):
    m = b.indoor & (b.kind == FLOOR) & (b.exit_id < 0)
    lab, n = _components(b, m)
    sizes = np.bincount(lab[lab >= 0], minlength=n)
    keep = np.nonzero(sizes >= 3)[0]  # 1-2 m2 pieces are furniture tops / nooks, not rooms
    remap = np.full(n + 1, -1, np.int64)
    remap[keep] = np.arange(len(keep))
    b.room = np.where(lab >= 0, remap[lab], -1)
    b.rooms = []
    for r in range(len(keep)):
        nodes = np.nonzero(b.room == r)[0]
        x, z = b.world_xz(nodes)
        lv = np.bincount(np.maximum(b.level[nodes], 0)).argmax() if len(nodes) else -1
        b.rooms.append({"id": r, "name": f"Room {r + 1}", "level": int(lv), "area_m2": int(len(nodes)),
                        "x": float(x.mean() + 0.5), "z": float(z.mean() + 0.5), "y": float(np.median(b.h[nodes])),
                        "bbox": [int(x.min()), int(z.min()), int(x.max()), int(z.max())], "seats": 0})


def _find_objects(b: Building, fixed: dict | None = None):
    """Label furniture from block roles + simple patterns (tables from fence posts / lone blocks).
    Blocks with a user correction (`fixed`) are not auto-labelled; corrected tables are added."""
    fixed = fixed or {}
    ox, oy, oz = b.origin
    shp, ids = b.shape, b.ids
    X, Y, Z = shp.shape
    roles = np.array(b.roles)
    role_grid = roles[ids]
    add = b.objects.append
    def keep(x, y, z):
        return (int(x), int(y), int(z)) not in fixed
    # beds (head part only), storage, workstations, kitchen, modded seats/tables
    for role, typ in (("bed", "bed"), ("storage", "storage"), ("workstation", "workstation"),
                      ("kitchen", "kitchen"), ("seat", "chair"), ("table", "table")):
        xs, ys, zs = np.nonzero(role_grid == role)
        for x, y, z in zip(xs, ys, zs):
            if role == "bed" and "part=head" not in b.states[ids[x, y, z]]:
                continue
            if keep(x, y, z):
                add({"type": typ, "x": int(x + ox), "y": int(y + oy), "z": int(z + oz)})
    for (x, y, z), k in fixed.items():
        if k == "table":
            add({"type": "table", "x": int(x + ox), "y": int(y + oy), "z": int(z + oz), "corrected": True})
    # tables: (a) fence post with carpet/pressure plate/slab on top, (b) lone block on a floor
    fence = np.array([("fence" in s and "gate" not in s) for s in b.states])[ids]
    topper = np.isin(role_grid, ["carpet", "plate"]) | (shp == SLAB)
    above = np.zeros_like(topper)
    above[:, :-1, :] = topper[:, 1:, :]
    tab_a = fence & above
    floor_below = np.zeros_like(fence)
    floor_below[:, 1:, :] = (shp[:, :-1, :] == SOLID)
    air_above = np.ones_like(fence)
    air_above[:, :-1, :] = (shp[:, 1:, :] == PASS)
    lone = (shp == SOLID) & (role_grid == "solid") & floor_below & air_above
    solid_nb = np.zeros(shp.shape, np.int8)
    for dx, dz in DIRS:
        sh = np.roll(np.roll(~((shp == PASS) | (shp == DOOR)), -dx, axis=0), -dz, axis=2)
        solid_nb += sh.astype(np.int8)
    tab_b = lone & (solid_nb <= 1)
    # only count tables standing on a floor under a roof (something solid 2+ blocks above)
    solid = ~((shp == PASS) | (shp == DOOR))
    above_count = np.cumsum(solid[:, ::-1, :], axis=1)[:, ::-1, :]  # solid cells at or above y
    roofed = np.zeros_like(solid)
    roofed[:, :-2, :] = above_count[:, 2:, :] > 0
    for mask in (tab_a, tab_b):
        xs, ys, zs = np.nonzero(mask & floor_below & roofed)
        for x, y, z in zip(xs, ys, zs):
            if keep(x, y, z):
                add({"type": "table", "x": int(x + ox), "y": int(y + oy), "z": int(z + oz)})


def _seat_nodes(b: Building):
    """Where seated people stand up: the floor node in front of each chair (or beside it)."""
    ox, oy, oz = b.origin
    X, Y, Z = b.shape.shape
    node_at = {}
    for i in range(b.n):
        node_at[(int(b.nx[i]), int(b.ny[i]), int(b.nz[i]))] = i
    seats = []
    for o in b.objects:
        if o["type"] != "chair":
            continue
        x, y, z = o["x"] - ox, o["y"] - oy, o["z"] - oz
        fx, fz = o.get("facing", [0, 0])
        cands = [(x + fx, z + fz)] + [(x + dx, z + dz) for dx, dz in DIRS.tolist()] + [(x - fx, z - fz)]
        for cx, cz in cands:
            n = node_at.get((cx, y - 1, cz))  # floor at the chair's support level
            if n is not None and b.kind[n] == FLOOR and not b.safe[n]:
                seats.append(n)
                o["node"] = int(n)
                if b.room[n] >= 0:
                    b.rooms[b.room[n]]["seats"] += 1
                break
    b.seat_nodes = np.array(seats, np.int64)


def _assign_sign_rooms(b: Building):
    for s in b.signs:
        n = s.get("node")
        if n is None:
            continue
        r = b.room[n]
        if r < 0:
            continue
        if s["tag"] == "ROOM" and s["arg"]:
            b.rooms[r]["name"] = s["arg"]
        elif s["tag"] == "PEOPLE":
            try:
                b.room_people[int(r)] = int(re.findall(r"\d+", s["arg"])[0])
            except (IndexError, ValueError):
                b.warnings.append(f"[PEOPLE] sign needs a number: '{s['arg']}'")
