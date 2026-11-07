"""Browser-only mode: the Python side of web/js/local/worker.js, run by Pyodide inside a Web Worker.

The worker reads the world the user picked (a folder or a .zip) in JavaScript and hands over only the files
needed, as bytes: level.dat, a few player files, and the region files under the map tiles being drawn or the
building being converted. Nothing is written to disk and nothing leaves the browser. Requests and answers
are the same as the local server's /api/* (see api.App), as JSON text or bytes.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath

from .api import App, regions_for
from .nbt import decompress, parse_nbt
from .world import RegionFile, World, render_map

APP = App()
WORLD: "MemoryWorld | None" = None


def _bytes(data) -> bytes:
    """bytes from bytes / memoryview, or from a JavaScript Uint8Array (a Pyodide JsProxy)."""
    return data.to_bytes() if hasattr(data, "to_bytes") and not isinstance(data, int) else bytes(data)


class MemoryWorld(World):
    """A World whose files are bytes in memory. Region files are added before they are read (add_region)."""

    def __init__(self, name: str, level_dat: bytes, regions, players=()):
        """`regions`: [(rx, rz, file size)] of every region file in the world (bytes come later);
        `players`: player .dat files, newest first."""
        self.path = PurePosixPath("/", name)
        self.source = None
        self.refresh = False
        self._regions: dict[tuple[int, int], RegionFile | None] = {}
        self._level_data = parse_nbt(decompress(_bytes(level_dat))).get("Data", {})
        self._players = []
        for p in players:
            try:
                self._players.append(parse_nbt(decompress(_bytes(p))))
            except Exception:
                continue
        self._sizes = {(int(rx), int(rz)): int(size) for rx, rz, size in regions}
        d = self._level_data
        self.info = {"name": name, "path": name, "version": d.get("Version", {}).get("Name", "?"),
                     "data_version": d.get("DataVersion"), "level_name": d.get("LevelName", name),
                     "regions": len(self._sizes), "size_mb": round(sum(self._sizes.values()) / 1e6, 1)}

    def _level(self) -> dict:
        return self._level_data

    def _player_dats(self):
        return iter(self._players)

    def regions(self) -> list[tuple[int, int]]:
        return sorted(k for k, size in self._sizes.items() if size >= 8192)

    def missing(self, keys) -> list[tuple[int, int]]:
        """Of these regions, the ones that exist in the world but have not been added yet."""
        return [k for k in keys if k in self._sizes and k not in self._regions]

    def keep_only(self, keys):
        """Drop region bytes that are not in `keys` (a new selection elsewhere)."""
        keys = set(keys)
        for k in [k for k in self._regions if k not in keys]:
            del self._regions[k]

    def add_region(self, rx: int, rz: int, data: bytes, external: dict | None = None):
        """`external`: {(cx, cz): bytes} of chunks stored in c.<cx>.<cz>.mcc files of this region."""
        self._regions[(rx, rz)] = _region_file(rx, rz, data, external)

    def region(self, rx: int, rz: int) -> RegionFile | None:
        return self._regions.get((rx, rz))


def _region_file(rx, rz, data, external=None) -> RegionFile | None:
    data = _bytes(data)
    if len(data) < 8192:
        return None
    ext = external or {}
    return RegionFile(PurePosixPath(f"r.{rx}.{rz}.mca"), data=data, external=lambda cx, cz: ext.get((cx, cz)))


def _external(mcc) -> dict:
    """{"c.<cx>.<cz>.mcc": bytes} from the worker -> {(cx, cz): bytes}."""
    out = {}
    for name, data in (mcc or {}).items():
        _, cx, cz, _ = name.split(".")
        out[(int(cx), int(cz))] = _bytes(data)
    return out


# ------------------------------------------------------------ worker calls ---
def open_world(name: str, level_dat, regions_json: str, players=()) -> str:
    global WORLD
    WORLD = MemoryWorld(name, level_dat, json.loads(regions_json), players)
    return json.dumps({"name": name, "info": WORLD.info, "regions": WORLD.regions(),
                       "player": WORLD.player_pos(), "spawn": WORLD.spawn()})


def tile(rx: int, rz: int, data, mcc=None) -> bytes:
    """Map tile of one region file: rgb[512, 512, 3] uint8 then top[512, 512] int16 (as /api/tile)."""
    rgb, top = render_map(_region_file(rx, rz, data, _external(mcc)))
    return rgb.tobytes() + top.astype("<i2").tobytes()


def regions_needed(body_json: str) -> str:
    """Region files a convert request still needs (and forget the ones it does not)."""
    keys = regions_for(json.loads(body_json))
    if WORLD is None:
        raise KeyError("Open a world first.")
    WORLD.keep_only(keys)
    return json.dumps(WORLD.missing(keys))


def add_region(rx: int, rz: int, data, mcc=None):
    WORLD.add_region(int(rx), int(rz), data, _external(mcc))


def call(op: str, body_json: str = "{}", report=None) -> str:
    """Everything else, as JSON text. `report(text)` gets progress of long jobs."""
    body = json.loads(body_json)
    if op == "convert":
        if WORLD is None:
            raise KeyError("Open a world first.")
        return json.dumps(APP.convert(WORLD, body))
    if op == "job":
        work = (APP.stress_job if body["kind"] == "stress" else APP.simulate_job)(body["body"])
        return json.dumps(work(report or (lambda text: None)))
    handlers = {
        "demo": lambda: APP.demo(body),
        "reanalyze": lambda: APP.reanalyze(body),
        "sim": lambda: APP.sim(body["id"]),
        "export_building": lambda: APP.export_building(body["id"]),
        "export_results": lambda: APP.export_results(body["id"], bool(body.get("paths"))),
    }
    if op not in handlers:
        raise ValueError(f"unknown request {op}")
    return json.dumps(handlers[op](), separators=(",", ":"))


def frames(sid: str) -> bytes:
    return APP.frames(sid)
