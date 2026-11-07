"""Classify Minecraft block states into simulation shapes, roles and colours.

Every distinct block state is classified once (worlds use palettes), so these
functions favour clarity over speed.

Shapes (how a block affects walking):
  PASS     no collision for people (air, torches, carpets, signs, plants, trapdoors)
  SOLID    full obstacle; its top can be stood on
  SLAB     bottom slab: stand on it at +0.5 m
  STAIRS   upright stairs: stand at +0.5 m; climbable along the facing direction
  BARRIER  tall thin obstacle you cannot stand on (fences, walls, panes, bars)
  OBST     furniture / small obstacle you cannot stand on (beds, chests, chairs)
  DOOR     doors and fence gates: passable portals
  LIQUID   water, lava, fire, powder snow: not walkable
"""
from __future__ import annotations

import re

PASS, SOLID, SLAB, STAIRS, BARRIER, OBST, DOOR, LIQUID = range(8)
SHAPE_NAMES = ["pass", "solid", "slab", "stairs", "barrier", "obstacle", "door", "liquid"]

FACING_VEC = {"east": (1, 0), "west": (-1, 0), "south": (0, 1), "north": (0, -1)}

_PASS_EXACT = {
    "air", "cave_air", "void_air", "light", "structure_void", "short_grass", "grass", "tall_grass",
    "fern", "large_fern", "dead_bush", "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet",
    "oxeye_daisy", "cornflower", "lily_of_the_valley", "wither_rose", "sunflower", "lilac", "rose_bush",
    "peony", "torchflower", "pink_petals", "wildflowers", "leaf_litter", "bush", "firefly_bush",
    "short_dry_grass", "tall_dry_grass", "cactus_flower", "pitcher_plant", "spore_blossom", "hanging_roots",
    "glow_lichen", "sculk_vein", "vine", "weeping_vines", "weeping_vines_plant", "twisting_vines",
    "twisting_vines_plant", "cave_vines", "cave_vines_plant", "sugar_cane", "kelp", "kelp_plant",
    "seagrass", "tall_seagrass", "nether_sprouts", "crimson_roots", "warped_roots", "brown_mushroom",
    "red_mushroom", "crimson_fungus", "warped_fungus", "wheat", "carrots", "potatoes", "beetroots",
    "melon_stem", "pumpkin_stem", "attached_melon_stem", "attached_pumpkin_stem", "torchflower_crop",
    "pitcher_crop", "nether_wart", "redstone_wire", "tripwire", "tripwire_hook", "lever", "rail",
    "powered_rail", "detector_rail", "activator_rail", "repeater", "comparator", "ladder", "scaffolding",
    "cobweb", "nether_portal", "end_portal", "end_gateway", "small_dripleaf", "snow", "lily_pad",
    "frogspawn", "lantern", "soul_lantern", "chain", "end_rod", "lightning_rod", "candle", "string",
    "sea_pickle", "moss_carpet", "pale_moss_carpet", "pale_hanging_moss", "resin_clump",
    "closed_eyeblossom", "open_eyeblossom", "iron_chain",
}
_PASS_SUFFIX = (
    "_sapling", "_tulip", "_torch", "_button", "_pressure_plate", "_sign", "_banner", "_carpet",
    "_candle", "_coral_fan", "_trapdoor", "_lantern", "_chain", "_propagule",
)
_PASS_RE = re.compile(r"^(dead_)?(tube|brain|bubble|fire|horn)_coral$")

_LIQUID = {"water", "lava", "bubble_column", "powder_snow", "fire", "soul_fire"}

_OBST_EXACT = {
    "chest", "trapped_chest", "ender_chest", "cauldron", "water_cauldron", "lava_cauldron",
    "powder_snow_cauldron", "composter", "anvil", "chipped_anvil", "damaged_anvil", "brewing_stand",
    "enchanting_table", "lectern", "grindstone", "stonecutter", "bell", "campfire", "soul_campfire",
    "cake", "flower_pot", "conduit", "decorated_pot", "hopper", "daylight_detector", "turtle_egg",
    "sniffer_egg", "dragon_egg", "cactus", "bamboo", "big_dripleaf", "big_dripleaf_stem",
    "pointed_dripstone", "amethyst_cluster", "large_amethyst_bud", "medium_amethyst_bud",
    "small_amethyst_bud", "sculk_sensor", "calibrated_sculk_sensor", "sculk_shrieker", "heavy_core",
    "sweet_berry_bush", "cocoa", "azalea", "flowering_azalea", "creaking_heart", "dried_ghast",
}
_OBST_SUFFIX = ("_bed", "_head", "_skull", "_candle_cake", "_shelf", "_golem_statue")
_OBST_PREFIX = ("potted_",)

_BARRIER_SUFFIX = ("_fence", "_pane", "_bars")
_BARRIER_EXACT = {"glass_pane", "iron_bars", "barrier"}
_WALL_DECOR = ("_wall_sign", "_wall_torch", "_wall_banner", "_wall_head", "_wall_skull",
               "_wall_fan", "_wall_hanging_sign")

# Modded furniture: map by keyword so furniture mods "just work".
_MOD_SEAT = ("chair", "sofa", "stool", "bench", "couch", "seat")
_MOD_TABLE = ("table", "desk", "counter")

# Roles (used for colouring, object detection and building.json)
ROLE_STORAGE = {"chest", "trapped_chest", "barrel", "bookshelf", "chiseled_bookshelf", "ender_chest"}
ROLE_WORKSTATION = {"crafting_table", "lectern", "loom", "cartography_table", "fletching_table",
                    "smithing_table", "stonecutter", "grindstone", "enchanting_table", "crafter"}
ROLE_KITCHEN = {"furnace", "smoker", "blast_furnace", "cauldron", "water_cauldron", "campfire"}


def parse_state(state: str):
    """'minecraft:oak_stairs[facing=north,half=bottom]' -> ('minecraft:oak_stairs', {...})."""
    if "[" in state:
        name, rest = state.split("[", 1)
        props = dict(p.split("=", 1) for p in rest.rstrip("]").split(",") if "=" in p)
        return name, props
    return state, {}


def state_key(name: str, props: dict | None) -> str:
    if not props:
        return name
    return name + "[" + ",".join(f"{k}={props[k]}" for k in sorted(props)) + "]"


def _base(name: str) -> tuple[str, str]:
    ns, _, base = name.partition(":")
    if not base:
        return "minecraft", ns
    return ns, base


def shape_of(name: str, props: dict) -> int:
    ns, b = _base(name)
    if ns != "minecraft":
        if any(k in b for k in _MOD_SEAT + _MOD_TABLE):
            return OBST
        if b.endswith("_stairs"):
            return STAIRS if props.get("half", "bottom") == "bottom" else SOLID
        if b.endswith("_slab"):
            return SLAB if props.get("type", "bottom") == "bottom" else SOLID
        if b.endswith("_door") or b.endswith("_fence_gate"):
            return DOOR
        return SOLID
    if b in _LIQUID:
        return LIQUID
    if b in _PASS_EXACT or b.endswith(_PASS_SUFFIX) or b.endswith(_WALL_DECOR) or _PASS_RE.match(b):
        if b == "snow" and int(props.get("layers", "1")) >= 4:
            return SLAB
        return PASS
    if b.endswith("_door") or b.endswith("_fence_gate"):
        return DOOR
    if b.endswith("_stairs"):
        return STAIRS if props.get("half", "bottom") == "bottom" else SOLID
    if b.endswith("_slab"):
        return SLAB if props.get("type", "bottom") == "bottom" else SOLID
    if b in _BARRIER_EXACT or b.endswith(_BARRIER_SUFFIX) or (b.endswith("_wall") and not b.endswith(_WALL_DECOR)):
        return BARRIER
    if b in _OBST_EXACT or b.endswith(_OBST_SUFFIX) or b.startswith(_OBST_PREFIX):
        return OBST
    return SOLID


def role_of(name: str, props: dict, shape: int) -> str:
    """Coarse semantic role, used for colouring and object detection."""
    ns, b = _base(name)
    if ns != "minecraft":
        if any(k in b for k in _MOD_SEAT):
            return "seat"
        if any(k in b for k in _MOD_TABLE):
            return "table"
    if shape == LIQUID:
        return "hazard" if b in ("lava", "fire", "soul_fire") else "water"
    if shape == DOOR:
        return "gate" if b.endswith("_fence_gate") else ("door_controlled" if b == "iron_door" else "door")
    if shape == STAIRS:
        return "stairs"
    if b.endswith("_bed"):
        return "bed"
    if b in ROLE_STORAGE:
        return "storage"
    if b in ROLE_WORKSTATION:
        return "workstation"
    if b in ROLE_KITCHEN:
        return "kitchen"
    if "glass" in b:
        return "glass"
    if b.endswith("_leaves"):
        return "leaves"
    if b in ("ladder", "scaffolding") or "vines" in b or b == "vine":
        return "climbable"
    if b.endswith("_carpet"):
        return "carpet"
    if b.endswith(("_pressure_plate",)):
        return "plate"
    if b.endswith(("_torch", "lantern")) or b in ("glowstone", "sea_lantern", "shroomlight", "jack_o_lantern",
                                                   "redstone_lamp", "froglight", "end_rod") or b.endswith("_froglight"):
        return "light"
    if b.endswith(("_sign", "_banner")):
        return "sign"
    if shape == BARRIER:
        return "barrier"
    if shape == OBST:
        return "furniture"
    if shape == SLAB:
        return "slab"
    if shape == PASS:
        return "air" if b in ("air", "cave_air", "void_air") else "decor"
    return "solid"


# ---------------------------------------------------------------- colours ---
_COLOR_WORDS = [
    ("light_blue", (102, 153, 216)), ("light_gray", (153, 153, 153)), ("white", (232, 232, 232)),
    ("orange", (216, 127, 51)), ("magenta", (178, 76, 216)), ("yellow", (229, 229, 51)),
    ("lime", (127, 204, 25)), ("pink", (242, 127, 165)), ("gray", (76, 76, 76)), ("cyan", (76, 127, 153)),
    ("purple", (127, 63, 178)), ("blue", (51, 76, 178)), ("brown", (102, 76, 51)), ("green", (102, 127, 51)),
    ("red", (153, 51, 51)), ("black", (25, 25, 25)),
    ("pale_oak", (230, 222, 214)), ("dark_oak", (67, 43, 20)), ("oak", (162, 130, 78)), ("spruce", (114, 84, 48)),
    ("birch", (196, 179, 123)), ("jungle", (160, 115, 80)), ("acacia", (168, 90, 50)),
    ("mangrove", (117, 54, 48)), ("cherry", (226, 178, 172)), ("bamboo", (194, 175, 82)),
    ("crimson", (101, 48, 70)), ("warped", (43, 104, 99)),
    ("deepslate", (80, 80, 82)), ("blackstone", (42, 36, 41)), ("basalt", (73, 72, 77)),
    ("quartz", (235, 229, 222)), ("calcite", (223, 224, 220)), ("smooth_stone", (158, 158, 158)),
    ("andesite", (136, 136, 137)), ("diorite", (188, 188, 189)), ("granite", (149, 103, 85)),
    ("tuff", (108, 109, 102)), ("prismarine", (99, 156, 151)), ("purpur", (169, 125, 169)),
    ("end_stone", (219, 222, 158)), ("red_sandstone", (186, 99, 29)), ("sandstone", (216, 203, 155)),
    ("red_sand", (190, 102, 33)), ("sand", (219, 207, 163)), ("gravel", (131, 127, 126)),
    ("stone_brick", (122, 121, 122)), ("tuff_brick", (98, 104, 97)), ("deepslate_brick", (70, 70, 71)),
    ("mud_brick", (137, 103, 79)), ("nether_brick", (44, 21, 26)), ("brick", (150, 97, 83)),
    ("netherrack", (97, 38, 38)), ("copper", (192, 107, 79)), ("iron", (220, 220, 220)),
    ("gold", (246, 208, 61)), ("diamond", (98, 237, 228)), ("emerald", (42, 203, 87)),
    ("lapis", (31, 67, 140)), ("redstone", (175, 24, 5)), ("obsidian", (15, 10, 24)),
    ("snow", (249, 254, 254)), ("ice", (145, 183, 253)), ("clay", (160, 166, 179)),
    ("terracotta", (152, 94, 67)), ("moss", (89, 109, 45)), ("leaves", (60, 110, 40)),
    ("grass_block", (95, 159, 53)), ("podzol", (91, 63, 24)), ("mycelium", (111, 98, 101)),
    ("dirt", (134, 96, 67)), ("mud", (60, 57, 60)), ("cobblestone", (122, 122, 122)), ("stone", (125, 125, 125)),
    ("bedrock", (85, 85, 85)), ("water", (63, 118, 228)), ("lava", (207, 92, 15)),
    ("bookshelf", (117, 94, 59)), ("glass", (200, 225, 235)), ("hay", (166, 136, 38)),
    ("wool", (220, 220, 220)), ("concrete", (180, 180, 180)), ("glowstone", (171, 131, 84)),
]


def color_of(name: str) -> tuple[int, int, int]:
    _, b = _base(name)
    for word, rgb in _COLOR_WORDS:
        if word in b:
            return rgb
    return (150, 140, 130)


def classify_palette(states: list[str]):
    """Return per-palette-entry arrays: shape, role, rgb, stair facing (dx,dz), stair shape."""
    import numpy as np

    n = len(states)
    shape = np.zeros(n, np.uint8)
    rgb = np.zeros((n, 3), np.uint8)
    facing = np.zeros((n, 2), np.int8)
    stair_shape = [""] * n
    roles = [""] * n
    for i, s in enumerate(states):
        name, props = parse_state(s)
        sh = shape_of(name, props)
        shape[i] = sh
        roles[i] = role_of(name, props, sh)
        rgb[i] = color_of(name)
        if "facing" in props and props["facing"] in FACING_VEC:
            facing[i] = FACING_VEC[props["facing"]]
        stair_shape[i] = props.get("shape", "") if sh == STAIRS else ""
    return shape, roles, rgb, facing, stair_shape
