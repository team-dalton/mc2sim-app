"""Continuous 2.5D evacuation simulation (social-force model).

Space: every walkable 1 m cell from the building model is a patch of continuous floor.
People move continuously inside and between cells; stairs change their elevation, so
several floors work naturally ("2.5D"). Walls are the edges of cells that have no
walkable neighbour.

Route choice: a distance field to the safe outdoor area on a 0.25 m sub-grid (Dijkstra),
with extra cost on stairs and near walls. Every `reroute_s` seconds crowded cells get extra cost
and each exit gets its expected queue time (people queueing / (width x exit_flow_psm)) added at
the edge of its queue area, so people pick a wider exit further away over a long queue; people
already in a queue are not charged for it and stay. Movement: Helbing-style social force
(driving force + person-person repulsion + wall repulsion) with hard collision limits.

Scenarios: exits, doors and staircases can be closed. Closed cells become walls. People either
know about closures from the start (`awareness="known"`, e.g. an alarm message) or only notice
them when they get within `notice_m` metres (`"on_arrival"`) and then turn back; people who know
pass it on to those right next to them (`tell_rate`), so a crowd heading for a closed exit turns
around from the front instead of jamming.

This is a prototype: parameters follow common literature values but are not calibrated.
"""
from __future__ import annotations

import dataclasses
import time
from dataclasses import asdict, dataclass, field

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree

from .analyze import FLOOR, OPP, STAIR, Building

S = 4                 # sub-cells per block side
CS = 1.0 / S          # sub-cell size (m)
AWARENESS = ("known", "on_arrival")
CLOSURE_KEYS = (("closed_exits", "exit", "exits"), ("closed_doors", "door", "doors"),
                ("closed_stairs", "stairs", "stairs"))
_VEC8 = np.array([(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)], float)


@dataclass
class SimParams:
    people: int | None = None        # None = seats + density x free floor area
    placement: str = "auto"          # auto | seats | random
    density: float = 0.08            # people per m2 for random placement when people is None
    speed_mean: float = 1.34         # m/s free walking speed (Weidmann)
    speed_sd: float = 0.26
    stair_factor: float = 0.55       # speed multiplier on stairs
    premove_max: float = 10.0        # s, people start moving at uniform(0, premove_max)
    radius: float = 0.26             # m body radius
    dt: float = 0.05
    max_time: float = 900.0
    record_hz: float = 5.0
    seed: int = 1
    reroute_s: float = 5.0           # s between congestion-aware route updates (0 = off)
    congestion_weight: float = 0.6   # extra route cost per person/m2 of crowding
    queue_m: float = 8.0             # people within this walk of an exit count as its queue (0 = ignore queues)
    exit_flow_psm: float = 1.3       # exit capacity used for expected queue time, people/s per m of width
    tau: float = 0.5                 # relaxation time
    # person-person repulsion; calibrated so a 1 m door passes ~1.2-1.4 people/s (SFPE ~1.3 p/s/m)
    a_person: float = 6.0            # m/s2
    b_person: float = 0.2            # m
    anisotropy: float = 0.35
    a_wall: float = 5.0
    b_wall: float = 0.1
    # scenario: ids of closed exits / doors / staircases (see Building.exits, .doors, .stairs)
    closed_exits: list = field(default_factory=list)
    closed_doors: list = field(default_factory=list)
    closed_stairs: list = field(default_factory=list)
    awareness: str = "known"         # known | on_arrival
    notice_m: float = 5.0            # on_arrival: walking distance at which a closure is noticed
    tell_rate: float = 1.0           # on_arrival: per second, people who know tell those touching them (0 = off)
    # assembly point [x, z] (world coords, on open ground around the building): once out, people walk there
    goal: list | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "SimParams":
        kw = {}
        for f in dataclasses.fields(cls):
            v = d.get(f.name)
            if v is None or v == "":
                continue
            if f.name.startswith("closed_"):
                v = sorted({int(x) for x in v})
            elif f.name == "goal":
                if len(v) < 2:
                    continue
                v = [float(v[0]), float(v[1])]
            elif f.name in ("placement", "awareness"):
                v = str(v)
            elif f.name in ("people", "seed"):
                v = int(v)
            else:
                v = float(v)
            kw[f.name] = v
        p = cls(**kw)
        p.validate()
        return p

    def validate(self):
        if self.placement not in ("auto", "seats", "random"):
            raise ValueError(f"placement must be auto, seats or random, not {self.placement!r}")
        if self.awareness not in AWARENESS:
            raise ValueError(f"awareness must be one of {AWARENESS}, not {self.awareness!r}")

    def has_closures(self) -> bool:
        return bool(self.closed_exits or self.closed_doors or self.closed_stairs)

    def without_closures(self) -> "SimParams":
        return dataclasses.replace(self, closed_exits=[], closed_doors=[], closed_stairs=[])


@dataclass
class SimResult:
    params: dict
    stats: dict
    frames: np.ndarray               # [F, P, 3] float32 world x, z, h (NaN when out)
    frame_times: np.ndarray
    node_max_density: np.ndarray     # per building node, people/m2
    node_congested_s: np.ndarray     # seconds with >= 2 people/m2
    start_nodes: np.ndarray
    exit_of: np.ndarray              # per person exit id (-1 none)
    out_time: np.ndarray             # per person evacuation time (NaN if not out)
    distance: np.ndarray = field(default=None)  # per node: route distance to safety (m)
    status: np.ndarray = field(default=None)    # per person: 0 out (at the assembly point if one is set),
                                                # 1 no way out, 2 still inside at the end, 3 out but not at the assembly point
    assembly_time: np.ndarray = field(default=None)  # per person: time they reached the assembly point (NaN if not)
    closed: np.ndarray = field(default=None)    # per node: closed by the scenario


# ---------------------------------------------------------------- scenarios ---
def closures(b: Building, p: SimParams) -> list[dict]:
    """The closed exits/doors/staircases of a scenario as [{type, id, name}]."""
    out = []
    for key, typ, attr in CLOSURE_KEYS:
        items = getattr(b, attr)
        for i in getattr(p, key):
            if not 0 <= i < len(items):
                raise ValueError(f"There is no {typ} with id {i} in this building (it has {len(items)}).")
            out.append({"type": typ, "id": int(i), "name": items[i]["name"]})
    return out


def closed_nodes(b: Building, p: SimParams) -> np.ndarray:
    m = np.zeros(b.n, bool)
    for c in closures(b, p):
        items = {"exit": b.exits, "door": b.doors, "stairs": b.stairs}[c["type"]]
        m[items[c["id"]]["nodes"]] = True
    return m


def with_closed(b: Building, closed: np.ndarray) -> Building:
    """Copy of the building in which closed cells are walls (no links in or out)."""
    nbr = b.nbr.copy()
    idx = np.nonzero(closed)[0]
    for k in range(4):
        m = nbr[idx, k]
        ok = m >= 0
        nbr[m[ok], OPP[k]] = -1
        nbr[idx, k] = -1
    return dataclasses.replace(b, nbr=nbr)


def _near_closed(b: Building, phys: Building, closed: np.ndarray, notice_m: float) -> np.ndarray:
    """Nodes within `notice_m` walking metres of a closed cell (where people notice the closure)."""
    n = b.n
    src = np.zeros(n, bool)
    nb = b.nbr[closed].ravel()
    src[nb[nb >= 0]] = True
    src &= ~closed
    if not src.any():
        return np.zeros(n, bool)
    rows = np.repeat(np.arange(n), 4)
    cols = phys.nbr.ravel()
    ok = cols >= 0
    g = csr_matrix((np.ones(ok.sum()), (rows[ok], cols[ok])), shape=(n, n))
    d = dijkstra(g, directed=True, indices=np.nonzero(src)[0], min_only=True, limit=notice_m + 0.01)
    return d <= notice_m


def _connected_to_safe(b: Building) -> np.ndarray:
    """Nodes whose walkable component contains part of the safe area."""
    n = b.n
    rows = np.repeat(np.arange(n), 4)
    cols = b.nbr.ravel()
    ok = cols >= 0
    g = csr_matrix((np.ones(ok.sum()), (rows[ok], cols[ok])), shape=(n, n))
    _, lab = connected_components(g, directed=False)
    return np.isin(lab, np.unique(lab[b.safe]))


def _goal_setup(b: Building, phys: Building, goal, p: SimParams):
    """Assembly point: nearest open-ground (safe) cell to `goal` [x, z] and a field over the safe area around it."""
    if not b.safe.any():
        raise ValueError("There is no open ground around the building, so an assembly point cannot be used.")
    gx, gz = float(goal[0]) - b.origin[0], float(goal[1]) - b.origin[2]
    cand = np.nonzero(b.safe)[0]
    d = np.hypot(b.nx[cand] + 0.5 - gx, b.nz[cand] + 0.5 - gz)
    if d.min() > 3.0:
        raise ValueError("The assembly point must be on the open ground around the building (the green area), "
                         "inside the selected area.")
    g = int(cand[d.argmin()])
    from .analyze import _components
    lab, _ = _components(phys, b.safe)
    area = lab == lab[g]
    targets = np.zeros(b.n, bool)
    targets[g] = True
    nav = NavGrid(b, p, nodes=area, targets=targets, queues=False)
    return g, area, nav


def _queue_areas(b: Building, queue_m: float) -> np.ndarray:
    """Per node: the exit whose queue area it is in (within `queue_m` walk of that exit), else -1."""
    area = np.full(b.n, -1, np.int64)
    ex_nodes = np.nonzero(b.exit_id >= 0)[0]
    if queue_m <= 0 or len(ex_nodes) == 0:
        return area
    n = b.n
    rows = np.repeat(np.arange(n), 4)
    cols = b.nbr.ravel()
    ok = (cols >= 0) & ~b.safe[rows] & ~b.safe[np.maximum(cols, 0)]
    g = csr_matrix((np.ones(ok.sum()), (rows[ok], cols[ok])), shape=(n, n))
    d, _, src = dijkstra(g, directed=True, indices=ex_nodes, min_only=True, limit=queue_m + 0.01,
                         return_predecessors=True)
    hit = np.isfinite(d) & (src >= 0)
    area[hit] = b.exit_id[src[hit]]
    return area


# ------------------------------------------------------------------ nav grid ---
class NavGrid:
    """Distance-to-safety fields on a 0.25 m sub-grid.

    `D` is the field people use when they know about all closures (closed cells cut out);
    `D_open` ignores closures (what people who have not noticed them yet follow)."""

    def __init__(self, b: Building, p: SimParams, closed: np.ndarray | None = None,
                 nodes: np.ndarray | None = None, targets: np.ndarray | None = None, queues: bool = True):
        """Default: route to the safe area over the building. `nodes` / `targets` (node masks) build a field
        over other cells to other targets instead (e.g. the open ground to an assembly point)."""
        self.b = b
        n = b.n
        # nodes in the navigation set: non-safe nodes connected to the safe area + safe nodes bordering them.
        # Sub-cells are stored compactly (slot * S*S + local index) so roofs and terrain cost nothing.
        if nodes is None:
            border = np.zeros(n, bool)
            for k in range(4):
                nb = b.nbr[:, k]
                ok = nb >= 0
                border[nb[ok & ~b.safe]] = True
            self.active = (~b.safe | border) & _connected_to_safe(b)
        else:
            self.active = nodes.copy()
        keep = np.nonzero(self.active)[0]
        self.slot = np.full(n, -1, np.int64)
        self.slot[keep] = np.arange(len(keep))
        ns = len(keep) * S * S
        sid = np.arange(ns, dtype=np.int64)
        node = keep[sid // (S * S)]
        loc = sid % (S * S)
        i, j = loc % S, loc // S
        self.sub_node = node
        act = self.active
        def other(k, ii, jj):
            m = b.nbr[node, k]
            ok = (m >= 0) & act[np.maximum(m, 0)]
            return np.where(ok, self.slot[np.maximum(m, 0)] * S * S + jj * S + ii, -1)
        nb4 = np.empty((ns, 4), np.int32)
        nb4[:, 0] = np.where(i < S - 1, sid + 1, other(0, 0, j))
        nb4[:, 1] = np.where(i > 0, sid - 1, other(1, S - 1, j))
        nb4[:, 2] = np.where(j < S - 1, sid + S, other(2, i, 0))
        nb4[:, 3] = np.where(j > 0, sid - S, other(3, i, S - 1))
        del i, j, loc
        self.nb4 = nb4
        nb8 = np.empty((ns, 8), np.int32)
        nb8[:, :4] = nb4
        for q, (a, c) in enumerate(((0, 2), (0, 3), (1, 2), (1, 3))):
            ea, ec = nb4[:, a], nb4[:, c]
            d1 = np.where(ea >= 0, nb4[np.maximum(ea, 0), c], -1)
            d2 = np.where(ec >= 0, nb4[np.maximum(ec, 0), a], -1)
            nb8[:, 4 + q] = np.where((ea >= 0) & (ec >= 0) & (d1 == d2), d1, -1)
        self.nb8 = nb8
        # costs
        near_wall = (nb8 < 0).any(axis=1)
        cost = np.ones(ns)
        cost[b.kind[node] == STAIR] /= p.stair_factor
        cost[near_wall] *= 1.3
        self.cost = cost
        rows, cols, lens_e = [], [], []
        lens = np.array([CS] * 4 + [CS * np.sqrt(2)] * 4)
        for k in range(8):
            t = nb8[:, k]
            ok = t >= 0
            rows.append(sid[ok].astype(np.int32))
            cols.append(t[ok])
            lens_e.append(np.full(ok.sum(), lens[k]))
        self.rows = np.concatenate(rows)
        self.cols = np.concatenate(cols)
        self.elen = np.concatenate(lens_e)
        del rows, cols, lens_e
        # build CSR once; remember where each edge lands so weights can be updated in place
        E = len(self.rows)
        self.g = csr_matrix((np.arange(1, E + 1, dtype=np.float64), (self.rows, self.cols)), shape=(ns, ns))
        self.perm = (self.g.data - 1).astype(np.int32)
        self.src = sid[(b.safe if targets is None else targets)[node]]
        self.closed_e = None
        if closed is not None and closed.any():
            cs = closed[node]
            self.closed_e = cs[self.rows] | cs[self.cols]
        # exit queue areas: nodes within queue_m walk of an exit (nearest exit wins). Dijkstra runs outward
        # from the exits, so an edge leaving an area is where a walker would *join* that queue.
        self.queue_area = _queue_areas(b, p.queue_m) if queues else np.full(n, -1, np.int64)
        qa = self.queue_area[node]
        ra, ca = qa[self.rows], qa[self.cols]
        self.q_edge = np.nonzero((ra >= 0) & (ra != ca))[0]
        self.q_edge_exit = ra[self.q_edge]
        self.exit_cap = np.array([max(1, e["width_m"]) * p.exit_flow_psm for e in b.exits] or [1.0])
        self.speed = p.speed_mean
        self.update(None, 0.0, open_field=True)

    def _dijkstra(self, w):
        if len(self.src) == 0:
            return np.full(len(self.cost), np.inf)
        self.g.data = w[self.perm]
        return dijkstra(self.g, directed=True, indices=self.src, min_only=True)

    def update(self, node_density, weight: float, open_field: bool = False, queue=None):
        """(Re)compute the distance fields; crowded cells cost more and long exit queues are avoided
        (`queue` = people per exit queue area)."""
        c = self.cost
        if node_density is not None and weight > 0:
            c = c * (1.0 + weight * np.maximum(0.0, node_density[self.sub_node] - 0.5))
        w = self.elen * 0.5 * (c[self.rows] + c[self.cols])
        if queue is not None and len(self.q_edge):
            wait_m = queue / self.exit_cap * self.speed        # expected queue time as metres of walking
            w[self.q_edge] += wait_m[self.q_edge_exit]
        if self.closed_e is None:
            self.D = self.D_open = self._dijkstra(w)
            return
        if open_field:
            self.D_open = self._dijkstra(w)
        w[self.closed_e] = np.inf
        self.D = self._dijkstra(w)

    def _unit_grad(self, ids, D):
        """Unit downhill direction of D at sub-cells `ids` (central differences, steepest-neighbour fallback)."""
        nb = self.nb4[ids]
        v = np.where(nb >= 0, D[np.maximum(nb, 0)], np.inf)
        d0 = D[ids]
        e, w, s, n_ = v[:, 0], v[:, 1], v[:, 2], v[:, 3]
        fin = np.isfinite
        with np.errstate(invalid="ignore"):
            gx = np.where(fin(e) & fin(w), (e - w) / (2 * CS),
                          np.where(fin(e), (e - d0) / CS, np.where(fin(w), (d0 - w) / CS, 0.0)))
            gz = np.where(fin(s) & fin(n_), (s - n_) / (2 * CS),
                          np.where(fin(s), (s - d0) / CS, np.where(fin(n_), (d0 - n_) / CS, 0.0)))
        dx = -np.nan_to_num(gx, nan=0.0, posinf=0.0, neginf=0.0)
        dz = -np.nan_to_num(gz, nan=0.0, posinf=0.0, neginf=0.0)
        weak = np.hypot(dx, dz) < 1e-6
        if weak.any():
            n8 = self.nb8[ids[weak]]
            vals = np.where(n8 >= 0, D[np.maximum(n8, 0)], np.inf)
            best = vals.argmin(axis=1)
            dx[weak], dz[weak] = _VEC8[best, 0], _VEC8[best, 1]
        norm = np.hypot(dx, dz) + 1e-12
        return dx / norm, dz / norm

    def direction(self, sid, D):
        """Desired walking direction at sub-cells `sid`: downhill on D, smoothed over the 4 neighbours."""
        if len(sid) == 0:
            return np.zeros((0, 2))
        ids = np.column_stack([sid, self.nb4[sid]])
        ok = ids >= 0
        safe_ids = np.maximum(ids, 0)
        ok &= np.isfinite(D[safe_ids])
        ok[:, 0] = True
        ux, uz = self._unit_grad(safe_ids.ravel(), D)
        sx = (ux.reshape(-1, 5) * ok).sum(axis=1)
        sz = (uz.reshape(-1, 5) * ok).sum(axis=1)
        norm = np.hypot(sx, sz) + 1e-12
        out = np.column_stack([sx / norm, sz / norm])
        out[~np.isfinite(D[sid])] = 0.0
        return out

    def node_distance(self, D=None):
        """Per building node: lowest route cost to safety (inf when not in the navigation set)."""
        out = np.full(self.b.n, np.inf)
        if len(self.cost):
            out[self.active] = (self.D if D is None else D).reshape(-1, S * S).min(axis=1)
        return out

    def subcell(self, x, z, nd):
        """Sub-cell id of positions (x, z) on nodes nd (all nodes must be in the navigation set)."""
        fx = np.clip(((x - self.b.nx[nd]) * S).astype(np.int64), 0, S - 1)
        fz = np.clip(((z - self.b.nz[nd]) * S).astype(np.int64), 0, S - 1)
        return self.slot[nd] * S * S + fz * S + fx


# ---------------------------------------------------------------- simulation ---
def _place_people(b: Building, reach: np.ndarray, p: SimParams, rng):
    """Start positions. `reach` must not depend on the scenario, so every scenario gets the same people."""
    free = reach & (b.kind == FLOOR) & b.indoor & (b.room >= 0)
    seats = [int(s) for s in b.seat_nodes if reach[s]]
    rng.shuffle(seats)
    used = set()
    starts: list[tuple[int, int]] = []  # (node, sub)
    def take(node):
        for sub in rng.permutation(S * S):
            key = (node, int(sub))
            if key not in used:
                used.add(key)
                starts.append(key)
                return True
        return False
    # explicit [PEOPLE n] room counts first
    for r, cnt in b.room_people.items():
        cand = np.nonzero(free & (b.room == r))[0]
        if len(cand):
            for node in rng.choice(cand, size=cnt, replace=True):
                take(int(node))
    if p.people is None:
        area = int(free.sum())
        target = len(starts) + max(len(seats), int(round(p.density * area)))
    else:
        target = int(p.people)
    target = min(target, 5000)
    if p.placement in ("auto", "seats"):
        for s in seats:
            if len(starts) >= target:
                break
            take(s)
    if p.placement in ("auto", "random"):
        cand = np.nonzero(free)[0]
        tries = 0
        while len(starts) < target and len(cand) and tries < target * 20:
            take(int(cand[rng.integers(len(cand))]))
            tries += 1
    return starts


def simulate(b: Building, p: SimParams | None = None, progress=None) -> SimResult:
    p = p or SimParams()
    p.validate()
    t_start = time.time()
    rng = np.random.default_rng(p.seed)
    closed = closed_nodes(b, p)
    any_closed = bool(closed.any())
    phys = with_closed(b, closed) if any_closed else b      # closed cells are walls for movement
    nav = NavGrid(b, p, closed if any_closed else None)
    reach_open = np.isfinite(nav.node_distance(nav.D_open))
    reach = np.isfinite(nav.node_distance(nav.D))
    starts = _place_people(b, reach_open & nav.active & ~b.safe, p, rng)
    P = len(starts)
    node = np.array([s[0] for s in starts], np.int64)
    sub = np.array([s[1] for s in starts], np.int64)
    px = b.nx[node] + ((sub % S) + 0.5 + rng.uniform(-0.3, 0.3, P)) * CS
    pz = b.nz[node] + ((sub // S) + 0.5 + rng.uniform(-0.3, 0.3, P)) * CS
    vx = np.zeros(P)
    vz = np.zeros(P)
    v0 = np.clip(rng.normal(p.speed_mean, p.speed_sd, P), 0.6, 2.0)
    t0 = rng.uniform(0, p.premove_max, P)
    out_time = np.full(P, np.nan)
    exit_of = np.full(P, -1, np.int64)
    last_exit = np.full(P, -1, np.int64)
    start_nodes = node.copy()
    r = p.radius
    ox, oy, oz = b.origin
    nbr = phys.nbr
    # diagonal neighbours per node: SE, NE, SW, NW
    diag = np.full((b.n, 4), -1, np.int64)
    for q, (a, c) in enumerate(((0, 2), (0, 3), (1, 2), (1, 3))):
        ea, ec = nbr[:, a], nbr[:, c]
        d1 = np.where(ea >= 0, nbr[np.maximum(ea, 0), c], -1)
        d2 = np.where(ec >= 0, nbr[np.maximum(ec, 0), a], -1)
        diag[:, q] = np.where((ea >= 0) & (ec >= 0) & (d1 == d2), d1, -1)
    walked = np.zeros(P)
    walk_exit = np.full(P, np.nan)     # walked distance when leaving the building
    reroute_every = int(round(p.reroute_s / p.dt)) if p.reroute_s > 0 else 0
    # who knows about the closures, and who has no way out at all
    everyone_knows = (not any_closed) or p.awareness == "known"
    near = None if everyone_knows else _near_closed(b, phys, closed, p.notice_m)
    informed = np.ones(P, bool) if everyone_knows else near[node]
    stuck = ~reach_open[node] | (informed & ~reach[node])
    active = ~stuck
    turned_back = 0
    p_tell = 0.0 if everyone_knows else 1.0 - np.exp(-p.tell_rate * p.dt)
    dist0 = nav.node_distance()
    # assembly point: after leaving the building people walk there and join the group
    goal_node, goal_area, gnav = _goal_setup(b, phys, p.goal, p) if p.goal else (None, None, None)
    outside = np.zeros(P, bool)        # left the building, walking to the assembly point
    assembled = np.zeros(P, bool)
    lost = np.zeros(P, bool)           # out, but the assembly point cannot be reached from where they came out
    asm_time = np.full(P, np.nan)
    asm_radius = 2.0 + 1.3 * np.sqrt(max(P, 1) / (2 * np.pi))   # area the group fills (~2 people/m2)
    asm_tree = None

    door_pass = {}
    prev_door = b.door_id[node].copy()
    stair_of = b.stair_id if b.stair_id is not None else np.full(b.n, -1, np.int64)
    stair_pass: dict[int, int] = {}
    last_stair = stair_of[node].copy()
    n_levels = len(b.levels)
    level_last = np.zeros(n_levels + 1)                    # last slot: cells between floors (landings, half-levels)
    frames, ftimes = [], []
    node_max = np.zeros(b.n)
    node_cong = np.zeros(b.n)
    rec_every = max(1, int(round(1.0 / (p.record_hz * p.dt))))
    rec_dt = rec_every * p.dt
    steps = int(p.max_time / p.dt)
    t = 0.0

    def elevation(node, px, pz):
        hh = b.h[node].copy()
        st = b.kind[node] == STAIR
        if st.any():
            fx = px[st] - b.nx[node[st]]
            fz = pz[st] - b.nz[node[st]]
            ux, uz = b.uphill[node[st], 0], b.uphill[node[st], 1]
            tt = np.where(ux == 1, fx, np.where(ux == -1, 1 - fx, np.where(uz == 1, fz, 1 - fz)))
            hh[st] = b.ny[node[st]] + oy + np.clip(tt, 0, 1)
        return hh

    subcell = nav.subcell

    for step in range(steps + 1):
        idx = np.nonzero(active)[0]
        if step % rec_every == 0:
            fr = np.full((P, 3), np.nan, np.float32)
            shown = np.nonzero(active | stuck | assembled | lost)[0]   # people who stopped stay visible
            if len(shown):
                hh = elevation(node[shown], px[shown], pz[shown])
                fr[shown] = np.column_stack([px[shown] + ox, pz[shown] + oz, hh])
                cnt = np.bincount(node[shown], minlength=b.n)
                np.maximum(node_max, cnt, out=node_max)
                node_cong += (cnt >= 2) * rec_dt
            inside = idx[~outside[idx]]
            if len(inside):
                lv = b.level[node[inside]]
                for L in np.unique(np.where(lv >= 0, lv, n_levels)):
                    level_last[L] = t
            frames.append(fr)
            ftimes.append(t)
            if progress and step % (rec_every * 25) == 0:
                progress(t, int(np.isfinite(out_time).sum()), P)
        if len(idx) == 0:
            break
        if reroute_every and step > 0 and step % reroute_every == 0:
            qa = nav.queue_area[node[idx]]
            queue = np.bincount(qa[qa >= 0], minlength=len(nav.exit_cap)).astype(float)
            nav.update(np.bincount(node[idx], minlength=b.n).astype(float), p.congestion_weight,
                       open_field=not informed[idx].all(), queue=queue)
        x, z, nd = px[idx], pz[idx], node[idx]
        moving = t >= t0[idx]
        # desired direction from the distance field (people who have not noticed a closure use the open one;
        # people already out follow the field to the assembly point)
        know = informed[idx]
        e = np.zeros((len(idx), 2))
        to_goal = outside[idx] & goal_area[nd] if gnav is not None else np.zeros(len(idx), bool)
        main = (nav.slot[nd] >= 0) & ~to_goal
        mk, mu = main & know, main & ~know
        if mk.any():
            e[mk] = nav.direction(subcell(x[mk], z[mk], nd[mk]), nav.D)
        if mu.any():
            e[mu] = nav.direction(subcell(x[mu], z[mu], nd[mu]), nav.D_open)
        if to_goal.any():
            e[to_goal] = gnav.direction(gnav.subcell(x[to_goal], z[to_goal], nd[to_goal]), gnav.D)
        speed = v0[idx] * np.where(b.kind[nd] == STAIR, p.stair_factor, 1.0) * moving
        ax = (speed * e[:, 0] - vx[idx]) / p.tau
        az = (speed * e[:, 1] - vz[idx]) / p.tau
        # person-person forces (same floor only: elevation scaled up)
        hh = elevation(nd, x, z)
        corr_x = np.zeros(len(idx))
        corr_z = np.zeros(len(idx))
        told = None
        if len(idx) > 1:
            tree = cKDTree(np.column_stack([x, z, hh * 3.0]))
            pairs = tree.query_pairs(2 * r + 0.8, output_type="ndarray")
            if len(pairs):
                a_, c_ = pairs[:, 0], pairs[:, 1]
                if p_tell > 0 and not know.all():
                    ka, kc = know[a_], know[c_]
                    cand = np.unique(np.concatenate([c_[ka & ~kc], a_[kc & ~ka]]))
                    told = cand[rng.random(len(cand)) < p_tell]
                dx_, dz_ = x[a_] - x[c_], z[a_] - z[c_]
                d = np.hypot(dx_, dz_) + 1e-9
                nxv, nzv = dx_ / d, dz_ / d
                f = p.a_person * np.exp((2 * r - d) / p.b_person)
                # anisotropy: people react more to those in front of them
                def aniso(i_, sgn):
                    cosphi = -sgn * (nxv * e[i_, 0] + nzv * e[i_, 1])
                    return p.anisotropy + (1 - p.anisotropy) * (1 + cosphi) / 2
                fa, fc = f * aniso(a_, 1), f * aniso(c_, -1)
                n_ = len(idx)
                ax += np.bincount(a_, fa * nxv, n_) - np.bincount(c_, fc * nxv, n_)
                az += np.bincount(a_, fa * nzv, n_) - np.bincount(c_, fc * nzv, n_)
                ov = np.maximum(0, 2 * r - d) * 0.5
                corr_x += np.bincount(a_, ov * nxv, n_) - np.bincount(c_, ov * nxv, n_)
                corr_z += np.bincount(a_, ov * nzv, n_) - np.bincount(c_, ov * nzv, n_)
        # wall forces from the 8 surrounding blocks
        bx, bz = b.nx[nd], b.nz[nd]
        neigh = [(nbr[nd, 0], 1, 0), (nbr[nd, 1], -1, 0), (nbr[nd, 2], 0, 1), (nbr[nd, 3], 0, -1),
                 (diag[nd, 0], 1, 1), (diag[nd, 1], 1, -1), (diag[nd, 2], -1, 1), (diag[nd, 3], -1, -1)]
        for m, ddx, ddz in neigh:
            closed_face = m < 0
            if not closed_face.any():
                continue
            qx = np.clip(x, bx + ddx, bx + ddx + 1)
            qz = np.clip(z, bz + ddz, bz + ddz + 1)
            wx, wz = x - qx, z - qz
            d = np.hypot(wx, wz) + 1e-9
            f = np.where(closed_face & (d < 1.0), p.a_wall * np.exp((r - d) / p.b_wall), 0.0)
            ax += f * wx / d
            az += f * wz / d
        nvx = vx[idx] + ax * p.dt
        nvz = vz[idx] + az * p.dt
        vmax = 1.5 * np.maximum(speed, 0.05)
        sp = np.hypot(nvx, nvz)
        scale = np.where(sp > vmax, vmax / (sp + 1e-12), 1.0)
        nvx *= scale
        nvz *= scale
        mx = np.clip(nvx * p.dt + corr_x, -0.3, 0.3)
        mz = np.clip(nvz * p.dt + corr_z, -0.3, 0.3)
        x0_, z0_ = x, z
        x, z, nd, nvx, nvz = _move(b, nbr, diag, x, z, nd, mx, mz, nvx, nvz, r)
        walked[idx] += np.hypot(x - x0_, z - z0_)
        px[idx], pz[idx], node[idx], vx[idx], vz[idx] = x, z, nd, nvx, nvz
        # bookkeeping: exits, doors, staircases, arrivals
        ex = b.exit_id[nd]
        last_exit[idx] = np.where(ex >= 0, ex, last_exit[idx])
        dr = b.door_id[nd]
        newdoor = (dr >= 0) & (dr != prev_door[idx])
        for d_ in dr[newdoor]:
            door_pass.setdefault(int(d_), []).append(t)
        prev_door[idx] = dr
        sk = stair_of[nd]
        newstair = (sk >= 0) & (sk != last_stair[idx])
        for s_ in sk[newstair]:
            stair_pass[int(s_)] = stair_pass.get(int(s_), 0) + 1
        last_stair[idx] = np.where(sk >= 0, sk, last_stair[idx])
        arrived = b.safe[nd] & ~outside[idx]
        if arrived.any():
            ai = idx[arrived]
            out_time[ai] = t
            exit_of[ai] = last_exit[ai]
            walk_exit[ai] = walked[ai]
            if gnav is None:
                active[ai] = False
            else:
                outside[ai] = True
                cant = ai[~goal_area[node[ai]]]
                active[cant] = False
                lost[cant] = True
        # assembly point: arrive at the centre, or join the group once touching someone already there
        if gnav is not None:
            o = outside[idx] & active[idx] & goal_area[nd]
            if o.any():
                oi = np.nonzero(o)[0]
                dg = gnav.D[gnav.subcell(x[oi], z[oi], nd[oi])]
                join = dg <= 1.0
                near_g = (dg <= asm_radius) & ~join
                if asm_tree is not None and near_g.any():
                    dd, _ = asm_tree.query(np.column_stack([x[oi[near_g]], z[oi[near_g]]]))
                    join[np.nonzero(near_g)[0][dd <= 2 * r + 0.1]] = True
                if join.any():
                    ji = idx[oi[join]]
                    active[ji] = False
                    assembled[ji] = True
                    asm_time[ji] = t
                    vx[ji] = vz[ji] = 0.0
                    asm_tree = cKDTree(np.column_stack([px[assembled], pz[assembled]]))
        # closures: people notice them nearby, turn back, or find there is no way out
        if any_closed:
            still = ~arrived & ~outside[idx]
            if not everyone_knows:
                newly = still & ~know & near[nd]
                if told is not None and len(told):
                    newly[told] |= still[told]
                if newly.any():
                    s2 = subcell(x[newly], z[newly], nd[newly])
                    turned_back += int((nav.D[s2] > nav.D_open[s2] + 2.0).sum())
                    informed[idx[newly]] = True
            trapped_now = still & informed[idx] & ~reach[nd]
            if trapped_now.any():
                ti = idx[trapped_now]
                active[ti] = False
                stuck[ti] = True
                vx[ti] = vz[ti] = 0.0
        t += p.dt

    status = np.where(np.isfinite(out_time), 0, np.where(stuck, 1, 2)).astype(np.int8)
    if gnav is not None:
        status[np.isfinite(out_time) & ~assembled] = 3
    stats = _stats(b, p, out_time, exit_of, stuck, door_pass, stair_pass, level_last, node_cong, node_max, P, t,
                   time.time() - t_start, start_nodes, walk_exit, turned_back)
    if gnav is not None:
        stats["assembly"] = _assembly_stats(b, goal_node, asm_time, out_time, lost, asm_radius, P)
    return SimResult(params=asdict(p), stats=stats, frames=np.array(frames, np.float32),
                     frame_times=np.array(ftimes), node_max_density=node_max, node_congested_s=node_cong,
                     start_nodes=start_nodes, exit_of=exit_of, out_time=out_time, distance=dist0,
                     status=status, closed=closed, assembly_time=asm_time)


def _curve(times):
    times = np.sort(times[np.isfinite(times)])
    if not len(times):
        return []
    return [[s, int(np.searchsorted(times, s, side="right"))] for s in range(0, int(np.ceil(times[-1])) + 2)]


def _assembly_stats(b, g, asm_time, out_time, lost, radius, P):
    done = np.isfinite(asm_time)
    tt = np.sort(asm_time[done])
    def pct(q):
        k = int(np.ceil(q / 100 * P)) - 1
        return round(float(tt[max(k, 0)]), 1) if P and k < len(tt) else None
    walk_out = asm_time[done] - out_time[done]
    return {"x": float(b.nx[g] + b.origin[0] + 0.5), "z": float(b.nz[g] + b.origin[2] + 0.5), "y": float(b.h[g]),
            "radius_m": round(float(radius), 1), "reached": int(done.sum()),
            "not_reached": int((np.isfinite(out_time) & ~done).sum()), "cannot_reach": int(lost.sum()),
            "total_time_s": round(float(tt[-1]), 1) if len(tt) else None, "t50_s": pct(50), "t90_s": pct(90),
            "mean_extra_s": round(float(walk_out.mean()), 1) if len(walk_out) else None,
            "curve": _curve(asm_time)}


def _move(b, nbr, diag, x, z, nd, mx, mz, vx, vz, r):
    """Axis-separated movement with collision against closed cell faces and corners."""
    # x axis
    nx_new = x + mx
    bx = b.nx[nd]
    east, west = nx_new >= bx + 1, nx_new < bx
    te, tw = nbr[nd, 0], nbr[nd, 1]
    nd = np.where(east & (te >= 0), te, np.where(west & (tw >= 0), tw, nd))
    blocked = (east & (te < 0)) | (west & (tw < 0))
    nx_new = np.where(east & (te < 0), bx + 1 - r, np.where(west & (tw < 0), bx + r, nx_new))
    vx = np.where(blocked, 0.0, vx)
    bx = b.nx[nd]
    nx_new = np.where(nbr[nd, 0] < 0, np.minimum(nx_new, bx + 1 - r), nx_new)
    nx_new = np.where(nbr[nd, 1] < 0, np.maximum(nx_new, bx + r), nx_new)
    # z axis
    nz_new = z + mz
    bz = b.nz[nd]
    south, north = nz_new >= bz + 1, nz_new < bz
    ts, tn = nbr[nd, 2], nbr[nd, 3]
    nd = np.where(south & (ts >= 0), ts, np.where(north & (tn >= 0), tn, nd))
    blocked = (south & (ts < 0)) | (north & (tn < 0))
    nz_new = np.where(south & (ts < 0), bz + 1 - r, np.where(north & (tn < 0), bz + r, nz_new))
    vz = np.where(blocked, 0.0, vz)
    bz = b.nz[nd]
    nz_new = np.where(nbr[nd, 2] < 0, np.minimum(nz_new, bz + 1 - r), nz_new)
    nz_new = np.where(nbr[nd, 3] < 0, np.maximum(nz_new, bz + r), nz_new)
    # x may need re-clamping after a z move into a narrower cell
    bx = b.nx[nd]
    nx_new = np.where(nbr[nd, 0] < 0, np.minimum(nx_new, bx + 1 - r), nx_new)
    nx_new = np.where(nbr[nd, 1] < 0, np.maximum(nx_new, bx + r), nx_new)
    nx_new = np.clip(nx_new, bx, bx + 0.999)
    nz_new = np.clip(nz_new, bz, bz + 0.999)
    # corners of closed diagonal cells
    for q, (cx, cz) in enumerate(((1, 1), (1, 0), (0, 1), (0, 0))):
        closed = diag[nd, q] < 0
        px_, pz_ = bx + cx, bz + cz
        wx, wz = nx_new - px_, nz_new - pz_
        d = np.hypot(wx, wz) + 1e-9
        push = closed & (d < r)
        if push.any():
            nx_new = np.where(push, px_ + wx / d * r, nx_new)
            nz_new = np.where(push, pz_ + wz / d * r, nz_new)
            nx_new = np.clip(nx_new, bx, bx + 0.999)
            nz_new = np.clip(nz_new, bz, bz + 0.999)
    return nx_new, nz_new, nd, vx, vz


def _stats(b, p, out_time, exit_of, stuck, door_pass, stair_pass, level_last, node_cong, node_max, P, t_end,
           wall_s, start_nodes, walked, turned_back):
    done = np.isfinite(out_time)
    times = np.sort(out_time[done])
    curve = []
    if len(times):
        for s in range(0, int(np.ceil(times[-1])) + 2):
            curve.append([s, int(np.searchsorted(times, s, side="right"))])

    def peak_flow(ts, window=10.0):
        ts = np.sort(np.asarray(ts))
        if len(ts) == 0:
            return 0.0
        j = np.searchsorted(ts, ts + window)
        return float((j - np.arange(len(ts))).max() / window)

    shut = {(c["type"], c["id"]) for c in closures(b, p)}
    exits = []
    for e in b.exits:
        m = exit_of == e["id"]
        ts = out_time[m & done]
        pf = peak_flow(ts)
        exits.append({"id": e["id"], "name": e["name"], "width_m": e["width_m"], "people": int(m.sum()),
                      "first_s": round(float(ts.min()), 1) if len(ts) else None,
                      "last_s": round(float(ts.max()), 1) if len(ts) else None,
                      "peak_flow_ps": round(pf, 2), "peak_specific_flow_psm": round(pf / max(1, e["width_m"]), 2),
                      "closed": ("exit", e["id"]) in shut})
    doors = []
    for d in b.doors:
        ts = door_pass.get(d["id"], [])
        doors.append({"id": d["id"], "name": d.get("name", f"Door {d['id'] + 1}"), "people": len(ts),
                      "peak_flow_ps": round(peak_flow(ts), 2), "width_m": d["width_m"], "is_exit": d["is_exit"],
                      "closed": ("door", d["id"]) in shut})
    stairs = [{"id": s["id"], "name": s["name"], "label": s["label"], "people": int(stair_pass.get(s["id"], 0)),
               "closed": ("stairs", s["id"]) in shut} for s in b.stairs]
    # congestion hotspots: group congested nodes by room/door
    hot = []
    order = np.argsort(-node_cong)
    for n in order[:200]:
        if node_cong[n] <= 0:
            break
        hot.append({"x": float(b.nx[n] + b.origin[0] + 0.5), "z": float(b.nz[n] + b.origin[2] + 0.5),
                    "y": float(b.h[n]), "congested_s": round(float(node_cong[n]), 1),
                    "max_density": float(node_max[n]), "door": int(b.door_id[n]), "room": int(b.room[n])})
    levels = []
    floors = [(L, lv["name"]) for L, lv in enumerate(b.levels)] + [(-1, "Between floors")]
    for L, name in floors:
        on = b.level[start_nodes] == L
        started = int(on.sum())
        if L < 0 and not started:
            continue
        levels.append({"id": L, "name": name, "people_start": started, "trapped": int((on & stuck).sum()),
                       "clear_s": round(float(level_last[L]), 1) if started else 0.0})
    def pct(q):  # time by which q% of *all* people are out (None if that never happens)
        k = int(np.ceil(q / 100 * P)) - 1
        return round(float(times[max(k, 0)]), 1) if P and k < len(times) else None
    return {
        "people": int(P), "evacuated": int(done.sum()), "trapped_no_route": int(stuck.sum()),
        "not_out_by_end": int(P - done.sum() - stuck.sum()),
        "total_time_s": round(float(times[-1]), 1) if len(times) else None,
        "t50_s": pct(50), "t90_s": pct(90), "t95_s": pct(95),
        "mean_walk_m": round(float(walked[done].mean()), 1) if done.any() else None,
        "max_walk_m": round(float(walked[done].max()), 1) if done.any() else None,
        "turned_back": int(turned_back),
        "scenario": {"closed": closures(b, p), "awareness": p.awareness, "notice_m": p.notice_m},
        "curve": curve, "exits": exits, "doors": doors, "stairs": stairs, "levels": levels, "hotspots": hot[:30],
        "sim_end_s": round(t_end, 1), "compute_s": round(wall_s, 2),
    }
