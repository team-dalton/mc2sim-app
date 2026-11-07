"""Read Minecraft Java worlds: discovery, read-only snapshots, chunks, map tiles, extraction.

Supports the classic layout (`<world>/region/*.mca`) and the 26.x layout
(`<world>/dimensions/minecraft/overworld/region/*.mca`), chunk formats from
1.16 to 26.x, and both palette encodings ({Name, Properties} and {id, properties}).
The original save is never written to. `snapshot()` copies level.dat and player data; region files are
copied into the snapshot only when a building there is read (first `World.region()` call, or again
after "re-copy"). Map tiles of regions that were never copied are rendered straight from the save,
read-only and in memory, so browsing the map does not copy the whole world.
"""
from __future__ import annotations

import gzip
import json
import os
import platform
import shutil
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .blocks import color_of, parse_state, state_key
from .nbt import parse_nbt, read_nbt_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OVERWORLD_DIRS = ("dimensions/minecraft/overworld/region", "region")
SOURCE_FILE = ".source"   # in a snapshot: path of the save it was copied from
MAP_CACHE_VERSION = 3 # bump when tile rendering changes so stale tiles are ignored


def default_saves_dir() -> Path:
    env = os.environ.get("MC_SAVES")
    if env:
        return Path(env)
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        return home / "Library/Application Support/minecraft/saves"
    if system == "Windows":
        return Path(os.environ.get("APPDATA", home)) / ".minecraft/saves"
    return home / ".minecraft/saves"


def _region_dir(world: Path) -> Path | None:
    for d in OVERWORLD_DIRS:
        p = world / d
        if p.is_dir() and any(p.glob("*.mca")):
            return p
    return None


def world_info(path: Path) -> dict:
    info = {"name": path.name, "path": str(path), "version": "?", "data_version": None}
    try:
        d = read_nbt_file(path / "level.dat").get("Data", {})
        info["version"] = d.get("Version", {}).get("Name", "?")
        info["data_version"] = d.get("DataVersion")
        info["level_name"] = d.get("LevelName", path.name)
    except Exception as e:  # unreadable level.dat: still list the world
        info["error"] = str(e)
    rd = _region_dir(path)
    files = list(rd.glob("*.mca")) if rd else []
    info["regions"] = len(files)
    info["size_mb"] = round(sum(f.stat().st_size for f in files) / 1e6, 1)
    return info


def list_worlds(saves: Path | None = None) -> list[dict]:
    saves = saves or default_saves_dir()
    if not saves.is_dir():
        return []
    out = []
    for p in sorted(saves.iterdir()):
        if (p / "level.dat").exists():
            out.append(world_info(p))
    return out


def snapshot(name: str, saves: Path | None = None, refresh: bool = False) -> Path:
    """Copy the parts of a save we need into data/snapshots/<name>. Never writes to the save."""
    src = (saves or default_saves_dir()) / name
    if not (src / "level.dat").exists():
        raise FileNotFoundError(f"World not found: {src}")
    dst = DATA_DIR / "snapshots" / name
    if dst.exists() and (dst / SOURCE_FILE).exists() and not refresh:
        return dst
    dst.mkdir(parents=True, exist_ok=True)
    (dst / SOURCE_FILE).write_text(str(src.resolve()))
    wanted = ["level.dat", "players/data", "playerdata"]     # region files are copied on demand (World.region)
    for rel in wanted:
        s = src / rel
        if s.is_file():
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, dst / rel)
        elif s.is_dir():
            for f in s.iterdir():
                if not f.is_file():
                    continue
                t = dst / rel / f.name
                st = f.stat()
                if t.exists() and t.stat().st_size == st.st_size and t.stat().st_mtime >= st.st_mtime:
                    continue
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, t)
    return dst


# ------------------------------------------------------------------ regions ---
class RegionFile:
    def __init__(self, path: Path, data: bytes | None = None, external=None):
        """`data`: the file's bytes when they are already in memory (browser-only mode). `external(cx, cz)`:
        bytes of a chunk stored in its own c.<cx>.<cz>.mcc file; by default read next to `path`."""
        self.path = Path(path)
        self.data = self.path.read_bytes() if data is None else data
        self._external = external

    def _external_chunk(self, cx: int, cz: int) -> bytes | None:
        if self._external is not None:
            return self._external(cx, cz)
        ext = self.path.parent / f"c.{cx}.{cz}.mcc"
        return ext.read_bytes() if ext.exists() else None

    def chunk(self, lx: int, lz: int) -> dict | None:
        i = (lx & 31) + (lz & 31) * 32
        d = self.data
        if len(d) < 8192:
            return None
        off = int.from_bytes(d[i * 4:i * 4 + 3], "big")
        if off == 0:
            return None
        base = off * 4096
        if base + 5 > len(d):
            return None
        (length,) = struct.unpack_from(">i", d, base)
        ctype = d[base + 4]
        if ctype & 128:  # stored in external .mcc file
            _, rx, rz, _ = self.path.name.split(".")
            raw = self._external_chunk(int(rx) * 32 + lx, int(rz) * 32 + lz)
            if raw is None:
                return None
            ctype &= 127
        else:
            raw = d[base + 5: base + 4 + length]
        if ctype == 2:
            buf = zlib.decompress(raw)
        elif ctype == 1:
            buf = gzip.decompress(raw)
        elif ctype == 3:
            buf = raw
        else:
            raise ValueError(f"Unsupported chunk compression {ctype} (LZ4?) in {self.path.name}")
        return parse_nbt(buf)


def _palette_key(entry) -> str:
    if isinstance(entry, str):
        return entry
    if "" in entry:  # 26.x mixed-type lists wrap plain strings as {"": value}
        return _palette_key(entry[""])
    name = entry.get("Name") or entry.get("id") or entry.get("name") or "minecraft:air"
    props = entry.get("Properties") or entry.get("properties") or {}
    return state_key(name, {k: str(v) for k, v in props.items()})


def _unpack(longs: np.ndarray, bits: int, count: int) -> np.ndarray:
    u = longs.view(">u8").astype(np.uint64)
    per = 64 // bits
    shifts = (np.arange(per, dtype=np.uint64) * np.uint64(bits))
    vals = (u[:, None] >> shifts[None, :]) & np.uint64((1 << bits) - 1)
    return vals.reshape(-1)[:count].astype(np.int64)


def _sections(chunk: dict):
    """Yield (sectionY, palette_keys, indices[4096] or None) for each section."""
    level = chunk.get("Level")
    if level is not None:  # 1.16-1.17 format
        for s in level.get("Sections", []):
            pal = s.get("Palette")
            if not pal:
                continue
            keys = [_palette_key(p) for p in pal]
            data = s.get("BlockStates")
            bits = max(4, (len(keys) - 1).bit_length())
            yield s.get("Y", 0), keys, (_unpack(data, bits, 4096) if data is not None else None)
        return
    for s in chunk.get("sections", []):
        bs = s.get("block_states")
        if not bs:
            continue
        pal = bs.get("palette") or []
        if not pal:
            continue
        keys = [_palette_key(p) for p in pal]
        data = bs.get("data")
        if len(keys) == 1 or data is None:
            yield s.get("Y", 0), keys, None
        else:
            bits = max(4, (len(keys) - 1).bit_length())
            yield s.get("Y", 0), keys, _unpack(data, bits, 4096)


def _component_text(m) -> str:
    if isinstance(m, dict) and "" in m:
        return _component_text(m[""])
    if isinstance(m, dict):
        return (str(m.get("text", "")) + "".join(_component_text(e) for e in m.get("extra", []))).strip()
    if isinstance(m, list):
        return "".join(_component_text(e) for e in m)
    if isinstance(m, str):
        s = m.strip()
        if s[:1] in ("{", "[", '"'):
            try:
                return _component_text(json.loads(s))
            except Exception:
                return s
        return s
    return ""


def _sign_text(be: dict) -> str:
    lines = []
    for side in ("front_text", "back_text"):
        for m in (be.get(side) or {}).get("messages") or []:
            lines.append(_component_text(m))
    for k in ("Text1", "Text2", "Text3", "Text4"):
        if k in be:
            lines.append(_component_text(be[k]))
    return " ".join(t for t in lines if t).strip()


AIRLIKE = ("minecraft:air", "minecraft:cave_air", "minecraft:void_air", "minecraft:light",
           "minecraft:barrier", "minecraft:structure_void")


@dataclass
class Volume:
    """A box of blocks: ids[x, y, z] index into `states`; world coords = origin + index."""
    ids: np.ndarray
    states: list[str]
    origin: tuple[int, int, int]
    signs: list[dict] = field(default_factory=list)


class World:
    def __init__(self, path: Path, refresh: bool = False):
        """`path`: a snapshot made by snapshot() (or any world folder). `refresh`: region files that changed in
        the save since they were copied are copied again when next read."""
        self.path = Path(path)
        sf = self.path / SOURCE_FILE
        src = Path(sf.read_text().strip()) if sf.exists() else None
        self.source = src if src is not None and src.is_dir() else None
        base = self.source or self.path
        rel = next((Path(d) for root in (base, self.path) for d in OVERWORLD_DIRS
                    if (root / d).is_dir() and any((root / d).glob("*.mca"))), None)
        if rel is None:
            raise FileNotFoundError(f"No overworld region files in {base}")
        self.region_dir = self.path / rel                        # local copies
        self.source_region_dir = self.source / rel if self.source else None
        self.refresh = refresh
        self._regions: dict[tuple[int, int], RegionFile | None] = {}
        self.info = world_info(base)

    def _copy_region(self, rx: int, rz: int) -> Path:
        """Make sure the snapshot has an up-to-date copy of one region file (and its external .mcc chunks)."""
        name = f"r.{rx}.{rz}.mca"
        dst = self.region_dir / name
        if self.source_region_dir is None:
            return dst
        srcf = self.source_region_dir / name
        if not srcf.exists():
            return dst
        st = srcf.stat()
        stale = dst.exists() and self.refresh and (dst.stat().st_size != st.st_size or dst.stat().st_mtime < st.st_mtime)
        if not dst.exists() or stale:
            self.region_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(srcf, dst)
            for ext in self.source_region_dir.glob("c.*.*.mcc"):
                _, cx, cz, _ = ext.name.split(".")
                if int(cx) >> 5 == rx and int(cz) >> 5 == rz:
                    shutil.copy2(ext, self.region_dir / ext.name)
        return dst

    def map_region_file(self, rx: int, rz: int) -> Path:
        """The file a map tile is drawn from: the local copy if there is one, else the save itself (read-only)."""
        local = self.region_dir / f"r.{rx}.{rz}.mca"
        if local.exists() or self.source_region_dir is None:
            return local
        return self.source_region_dir / f"r.{rx}.{rz}.mca"

    def regions(self) -> list[tuple[int, int]]:
        out = []
        for f in (self.source_region_dir or self.region_dir).glob("r.*.*.mca"):
            _, rx, rz, _ = f.name.split(".")
            if f.stat().st_size >= 8192:
                out.append((int(rx), int(rz)))
        return sorted(out)

    def _level(self) -> dict:
        return read_nbt_file(self.path / "level.dat").get("Data", {})

    def _player_dats(self):
        """Parsed player files, newest first."""
        for sub in ("players/data", "playerdata"):
            for f in sorted((self.path / sub).glob("*.dat"), key=lambda f: f.stat().st_mtime, reverse=True):
                try:
                    yield read_nbt_file(f)
                except Exception:
                    continue

    def player_pos(self):
        try:
            d = self._level()
            if "Player" in d and "Pos" in d["Player"]:
                return [float(v) for v in d["Player"]["Pos"]]
        except Exception:
            pass
        for p in self._player_dats():
            if "Pos" in p:
                return [float(v) for v in p["Pos"]]
        return None

    def spawn(self):
        try:
            d = self._level()
            if "spawn" in d:
                return [int(v) for v in d["spawn"]["pos"]]
            return [d.get("SpawnX", 0), d.get("SpawnY", 64), d.get("SpawnZ", 0)]
        except Exception:
            return [0, 64, 0]

    def region(self, rx: int, rz: int) -> RegionFile | None:
        """Region for reading a building: copied into the snapshot first, then read from the copy."""
        key = (rx, rz)
        if key not in self._regions:
            p = self._copy_region(rx, rz)
            self._regions[key] = RegionFile(p) if p.exists() and p.stat().st_size >= 8192 else None
        return self._regions[key]

    def chunk(self, cx: int, cz: int) -> dict | None:
        r = self.region(cx >> 5, cz >> 5)
        return r.chunk(cx & 31, cz & 31) if r else None

    # -- map tiles
    def render_region_map(self, rx: int, rz: int):
        """Top-down colour map of one region (512x512, [z, x]). Returns (rgb uint8, top_y int16)."""
        f = self.map_region_file(rx, rz)   # read once into memory; never copied just for the map
        return render_map(RegionFile(f) if f.exists() and f.stat().st_size >= 8192 else None)

    # -- extraction
    def extract(self, x0: int, z0: int, x1: int, z1: int, y0: int = -64, y1: int = 319) -> Volume:
        """Read all blocks in the inclusive box into a Volume."""
        x0, x1 = sorted((int(x0), int(x1)))
        z0, z1 = sorted((int(z0), int(z1)))
        y0, y1 = sorted((int(y0), int(y1)))
        ids = np.zeros((x1 - x0 + 1, y1 - y0 + 1, z1 - z0 + 1), np.uint16)
        states = ["minecraft:air"]
        index = {"minecraft:air": 0}
        signs = []
        for cz in range(z0 >> 4, (z1 >> 4) + 1):
            for cx in range(x0 >> 4, (x1 >> 4) + 1):
                try:
                    c = self.chunk(cx, cz)
                except Exception:
                    c = None
                if not c:
                    continue
                bx, bz = cx * 16, cz * 16
                ax0, ax1 = max(x0, bx), min(x1, bx + 15)
                az0, az1 = max(z0, bz), min(z1, bz + 15)
                for sy, keys, idx in _sections(c):
                    by = sy * 16
                    ay0, ay1 = max(y0, by), min(y1, by + 15)
                    if ay0 > ay1:
                        continue
                    for k in keys:
                        if k not in index:
                            index[k] = len(states)
                            states.append(k)
                    gl = np.array([index[k] for k in keys], np.uint16)
                    block = (np.full((16, 16, 16), gl[0], np.uint16) if idx is None
                             else gl[idx].reshape(16, 16, 16))  # y, z, x
                    sub = block[ay0 - by:ay1 - by + 1, az0 - bz:az1 - bz + 1, ax0 - bx:ax1 - bx + 1]
                    ids[ax0 - x0:ax1 - x0 + 1, ay0 - y0:ay1 - y0 + 1, az0 - z0:az1 - z0 + 1] = sub.transpose(2, 0, 1)
                for be in c.get("block_entities", []) or c.get("Level", {}).get("TileEntities", []):
                    if "sign" in str(be.get("id", "")):
                        x, y, z = be.get("x"), be.get("y"), be.get("z")
                        if x0 <= x <= x1 and z0 <= z <= z1 and y0 <= y <= y1:
                            text = _sign_text(be)
                            if text:
                                signs.append({"x": x, "y": y, "z": z, "text": text})
        return Volume(ids=ids, states=states, origin=(x0, y0, z0), signs=signs)


def render_map(reg: RegionFile | None):
    """Top-down colour map of a region file (512x512, [z, x]). Returns (rgb uint8, top_y int16)."""
    rgb = np.zeros((512, 512, 3), np.uint8)
    top = np.full((512, 512), -9999, np.int16)
    if reg is None:
        return rgb, top
    cache: dict[str, tuple] = {}
    for lz in range(32):
        for lx in range(32):
            try:
                c = reg.chunk(lx, lz)
            except Exception:
                continue
            if c:
                _chunk_top(c, rgb, top, lx * 16, lz * 16, cache)
    valid = top > -9999
    north = np.vstack([top[:1], top[:-1]])
    diff = np.clip(top.astype(np.int32) - north.astype(np.int32), -3, 3)
    shade = np.where(valid, 1.0 + 0.07 * diff, 1.0)[..., None]
    rgb = np.clip(rgb.astype(np.float32) * shade, 0, 255).astype(np.uint8)
    return rgb, top


def _chunk_top(c, rgb, top, ox, oz, cache):
    sections = {sy: (keys, idx) for sy, keys, idx in _sections(c)}
    found = np.zeros((16, 16), bool)
    for sy in sorted(sections, reverse=True):
        keys, idx = sections[sy]
        air = np.array([k.split("[")[0] in AIRLIKE for k in keys])
        if idx is None:
            if air[0]:
                continue
            grid = np.zeros((16, 16, 16), np.int64)
        else:
            grid = idx.reshape(16, 16, 16)  # y, z, x
        solid = ~air[grid]
        cols = solid.any(axis=0) & ~found
        if not cols.any():
            continue
        ytop = 15 - np.argmax(solid[::-1], axis=0)
        zs, xs = np.nonzero(cols)
        for z, x in zip(zs, xs):
            k = keys[grid[ytop[z, x], z, x]]
            col = cache.get(k)
            if col is None:
                col = cache[k] = color_of(parse_state(k)[0])
            rgb[oz + z, ox + x] = col
            top[oz + z, ox + x] = sy * 16 + ytop[z, x]
        found |= cols
        if found.all():
            break


def load_map_tile(world: World, rx: int, rz: int, cache_key: str):
    """Cached region map tile. Returns (rgb[512,512,3], top[512,512]) indexed [z, x]."""
    cache = DATA_DIR / "cache" / cache_key
    cache.mkdir(parents=True, exist_ok=True)
    f = cache / f"map.v{MAP_CACHE_VERSION}.{rx}.{rz}.npz"
    src = world.map_region_file(rx, rz)
    if f.exists() and src.exists() and f.stat().st_mtime >= src.stat().st_mtime:
        d = np.load(f)
        return d["rgb"], d["top"]
    rgb, top = world.render_region_map(rx, rz)
    np.savez_compressed(f, rgb=rgb, top=top)
    return rgb, top
