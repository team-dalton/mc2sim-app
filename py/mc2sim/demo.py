"""Built-in demo: a small two-storey school generated in code (no Minecraft needed).

Layout (top view, x to the right, z down), ground floor:
  - 24 x 14 building with stone walls, oak-plank floors, glass windows
  - south wall: double oak door (main exit) ; east wall: single door (side exit)
  - interior wall at x=12 with a door between the classroom (west) and the hall (east)
  - classroom: 3 rows of stair chairs facing fence-post tables
  - hall: a straight staircase (oak_stairs facing north) up to the 1st floor
  - 1st floor: one big room with chairs, reached via the staircase opening
"""
from __future__ import annotations

import numpy as np

from mc2sim.world import Volume


class _Builder:
    def __init__(self, sx, sy, sz, origin):
        self.ids = np.zeros((sx, sy, sz), np.uint16)
        self.states = ["minecraft:air"]
        self.index = {"minecraft:air": 0}
        self.origin = origin
        self.signs = []

    def pid(self, s):
        if s not in self.index:
            self.index[s] = len(self.states)
            self.states.append(s)
        return self.index[s]

    def fill(self, x0, y0, z0, x1, y1, z1, s):
        self.ids[x0:x1 + 1, y0:y1 + 1, z0:z1 + 1] = self.pid(s)

    def set(self, x, y, z, s):
        self.ids[x, y, z] = self.pid(s)

    def sign(self, x, y, z, text):
        self.set(x, y, z, "minecraft:oak_sign[rotation=0,waterlogged=false]")
        ox, oy, oz = self.origin
        self.signs.append({"x": x + ox, "y": y + oy, "z": z + oz, "text": text})

    def volume(self):
        return Volume(ids=self.ids, states=self.states, origin=self.origin, signs=self.signs)


def door(b, x, y, z, facing="south"):
    b.set(x, y, z, f"minecraft:oak_door[facing={facing},half=lower,hinge=left,open=false,powered=false]")
    b.set(x, y + 1, z, f"minecraft:oak_door[facing={facing},half=upper,hinge=left,open=false,powered=false]")


def make_school(with_markers: bool = False) -> Volume:
    # grid: x 0..39, y 0..15, z 0..29 ; ground surface at y=3 (grass), building at x 8..31, z 6..19
    b = _Builder(40, 16, 30, origin=(100, -64, 200))
    b.fill(0, 0, 0, 39, 0, 29, "minecraft:bedrock")
    b.fill(0, 1, 0, 39, 2, 29, "minecraft:dirt")
    b.fill(0, 3, 0, 39, 3, 29, "minecraft:grass_block[snowy=false]")
    X0, X1, Z0, Z1 = 8, 31, 6, 19
    G = 3          # ground floor support layer (floor blocks at y=3, people stand at y=4)
    F1 = 8         # first floor support layer
    ROOF = 13
    b.fill(X0, G, Z0, X1, G, Z1, "minecraft:oak_planks")
    # outer walls
    for y in range(G + 1, ROOF):
        b.fill(X0, y, Z0, X1, y, Z0, "minecraft:stone_bricks")
        b.fill(X0, y, Z1, X1, y, Z1, "minecraft:stone_bricks")
        b.fill(X0, y, Z0, X0, y, Z1, "minecraft:stone_bricks")
        b.fill(X1, y, Z0, X1, y, Z1, "minecraft:stone_bricks")
    # windows
    for x in range(X0 + 2, X1 - 1, 3):
        b.fill(x, G + 2, Z0, x, G + 3, Z0, "minecraft:glass")
        b.fill(x, F1 + 2, Z0, x, F1 + 3, Z0, "minecraft:glass")
    # first floor slab and roof
    b.fill(X0, F1, Z0, X1, F1, Z1, "minecraft:oak_planks")
    b.fill(X0, ROOF, Z0, X1, ROOF, Z1, "minecraft:smooth_stone")
    # interior wall x=12+X0 on the ground floor
    IW = X0 + 12
    b.fill(IW, G + 1, Z0, IW, F1 - 1, Z1, "minecraft:white_concrete")
    door(b, IW, G + 1, Z0 + 9, facing="east")
    # exits: double door south, single door east
    door(b, X0 + 6, G + 1, Z1)
    door(b, X0 + 7, G + 1, Z1)
    door(b, X1, G + 1, Z0 + 4, facing="east")
    # classroom chairs (stairs facing north = back to the north, sitter faces south) + tables
    for row, z in enumerate((Z0 + 3, Z0 + 6, Z0 + 9)):
        for x in range(X0 + 2, X0 + 10, 2):
            b.set(x, G + 1, z, "minecraft:oak_stairs[facing=north,half=bottom,shape=straight,waterlogged=false]")
            b.set(x, G + 1, z + 1, "minecraft:oak_fence[east=false,north=false,south=false,west=false,waterlogged=false]")
            b.set(x, G + 2, z + 1, "minecraft:white_carpet")
    # staircase in the hall, climbing north from z=Z1-2 to reach F1
    sx = X1 - 3
    for i in range(F1 - G):
        y = G + 1 + i
        z = Z1 - 2 - i
        for w in (0, 1):
            b.set(sx + w, y, z, "minecraft:oak_stairs[facing=north,half=bottom,shape=straight,waterlogged=false]")
            b.fill(sx + w, y + 1, z, sx + w, y + 3, z, "minecraft:air")
    # opening in the first floor above the stairs
    top_z = Z1 - 2 - (F1 - G - 1)   # the top stair sits in the floor layer and lands on the floor north of it
    b.fill(sx, F1, top_z + 1, sx + 1, F1, Z1 - 1, "minecraft:air")
    # railing along the opening on the first floor
    b.fill(sx - 1, F1 + 1, top_z, sx - 1, F1 + 1, Z1 - 1, "minecraft:oak_fence[east=false,north=false,south=false,west=false,waterlogged=false]")
    # first-floor seats
    for x in range(X0 + 3, X0 + 15, 2):
        b.set(x, F1 + 1, Z0 + 4, "minecraft:spruce_stairs[facing=south,half=bottom,shape=straight,waterlogged=false]")
    # a bed and a chest upstairs
    b.set(X0 + 2, F1 + 1, Z1 - 2, "minecraft:red_bed[facing=north,occupied=false,part=foot]")
    b.set(X0 + 2, F1 + 1, Z1 - 3, "minecraft:red_bed[facing=north,occupied=false,part=head]")
    b.set(X0 + 4, F1 + 1, Z1 - 2, "minecraft:chest[facing=north,type=single,waterlogged=false]")
    # torches (should not block)
    b.set(X0 + 1, G + 3, Z0 + 1, "minecraft:torch")
    if with_markers:
        b.sign(X0 + 2, G + 1, Z0 + 1, "[ROOM Classroom A] [PEOPLE 12]")
        b.sign(X0 + 14, F1 + 1, Z0 + 1, "[ROOM Library]")
    return b.volume()
