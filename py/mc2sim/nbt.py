"""Minimal, fast reader for Minecraft's NBT format (read-only).

Large numeric arrays (block-state data, heightmaps) are returned as zero-copy
numpy views so that chunk decoding stays fast in pure Python.
"""
from __future__ import annotations

import gzip
import struct
import zlib

import numpy as np

_B = struct.Struct(">b")
_H = struct.Struct(">H")
_h = struct.Struct(">h")
_i = struct.Struct(">i")
_q = struct.Struct(">q")
_f = struct.Struct(">f")
_d = struct.Struct(">d")

_LIST_FMT = {1: "b", 2: "h", 3: "i", 4: "q", 5: "f", 6: "d"}
_LIST_SIZE = {1: 1, 2: 2, 3: 4, 4: 8, 5: 4, 6: 8}


def _parse(buf: bytes, pos: int, tag: int):
    """Parse one payload of type `tag` starting at `pos`. Returns (value, new_pos)."""
    if tag == 10:  # compound
        out = {}
        while True:
            t = buf[pos]
            pos += 1
            if t == 0:
                return out, pos
            (n,) = _H.unpack_from(buf, pos)
            pos += 2
            name = buf[pos:pos + n].decode("utf-8", "replace")
            pos += n
            out[name], pos = _parse(buf, pos, t)
    if tag == 8:  # string
        (n,) = _H.unpack_from(buf, pos)
        pos += 2
        return buf[pos:pos + n].decode("utf-8", "replace"), pos + n
    if tag == 1:
        return _B.unpack_from(buf, pos)[0], pos + 1
    if tag == 3:
        return _i.unpack_from(buf, pos)[0], pos + 4
    if tag == 9:  # list
        et = buf[pos]
        (n,) = _i.unpack_from(buf, pos + 1)
        pos += 5
        if n <= 0 or et == 0:
            return [], pos
        if et in _LIST_FMT:
            size = _LIST_SIZE[et] * n
            vals = list(struct.unpack_from(">%d%s" % (n, _LIST_FMT[et]), buf, pos))
            return vals, pos + size
        out = []
        for _ in range(n):
            v, pos = _parse(buf, pos, et)
            out.append(v)
        return out, pos
    if tag == 12:  # long array
        (n,) = _i.unpack_from(buf, pos)
        pos += 4
        return np.frombuffer(buf, dtype=">i8", count=n, offset=pos), pos + 8 * n
    if tag == 11:  # int array
        (n,) = _i.unpack_from(buf, pos)
        pos += 4
        return np.frombuffer(buf, dtype=">i4", count=n, offset=pos), pos + 4 * n
    if tag == 7:  # byte array
        (n,) = _i.unpack_from(buf, pos)
        pos += 4
        return np.frombuffer(buf, dtype=np.int8, count=n, offset=pos), pos + n
    if tag == 2:
        return _h.unpack_from(buf, pos)[0], pos + 2
    if tag == 4:
        return _q.unpack_from(buf, pos)[0], pos + 8
    if tag == 5:
        return _f.unpack_from(buf, pos)[0], pos + 4
    if tag == 6:
        return _d.unpack_from(buf, pos)[0], pos + 8
    raise ValueError(f"Unknown NBT tag {tag} at {pos}")


def parse_nbt(buf: bytes) -> dict:
    """Parse an uncompressed NBT document (root must be a compound)."""
    if not buf:
        return {}
    tag = buf[0]
    if tag != 10:
        raise ValueError("NBT root is not a compound")
    (n,) = _H.unpack_from(buf, 1)
    value, _ = _parse(buf, 3 + n, 10)
    return value


def decompress(data: bytes) -> bytes:
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    if data[:1] == b"\x78":
        return zlib.decompress(data)
    return data


def read_nbt_file(path) -> dict:
    with open(path, "rb") as f:
        return parse_nbt(decompress(f.read()))
