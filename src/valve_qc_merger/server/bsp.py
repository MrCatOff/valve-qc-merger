"""What a GoldSource map (BSP v30) precaches on its own.

Every brush model of the map takes a model slot ("*0" is the world itself,
"*1".. the doors, lifts, breakables...); entities name models and sprites
(``model`` / ``sprite`` keys) and sounds (``message`` of ambient_generic,
``noise*``...). Only values that are file names count; numeric sound
indices (doors) and sentences (``!name``) are the game DLL's own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.mapkit.bsp import ENTITIES as LUMP_ENTITIES
from valve_qc_merger.mapkit.bsp import MODELS as LUMP_MODELS
from valve_qc_merger.mapkit.bsp import RECORD, Bsp, BspError
from valve_qc_merger.mapkit.bsp import VERSION as BSP_VERSION

DMODEL_SIZE = RECORD[LUMP_MODELS]
_PAIR = re.compile(r'"([^"]*)"\s*"([^"]*)"')


@dataclass
class MapResources:
    name: str
    brush_models: int = 0  # world + submodels: each takes a model slot
    models: set[str] = field(default_factory=set)  # entity .mdl/.spr (game paths)
    sounds: set[str] = field(default_factory=set)  # entity .wav (relative to sound/)
    entities: int = 0
    wads: list[str] = field(default_factory=list)  # worldspawn "wad": file names

    @property
    def model_slots(self) -> int:
        return self.brush_models + len(self.models)


def entities(text: str) -> list[dict[str, str]]:
    """The entity lump as a list of key/value dictionaries."""
    out: list[dict[str, str]] = []
    for block in re.findall(r"\{([^{}]*)\}", text):
        out.append(dict(_PAIR.findall(block)))
    return out


def read_map_resources(path: Path) -> MapResources:
    bsp = Bsp.read(path)
    text = bsp.entity_text
    result = MapResources(name=Path(path).stem, brush_models=bsp.count(LUMP_MODELS))
    ents = entities(text)
    result.entities = len(ents)
    world = next((e for e in ents if e.get("classname") == "worldspawn"), {})
    result.wads = wad_names(world.get("wad", ""))
    for entity in ents:
        for key, value in entity.items():
            low = value.lower().replace("\\", "/")
            if low.endswith((".mdl", ".spr")) and not low.startswith("*"):
                result.models.add(low)
            elif low.endswith(".wav") and not low.startswith("!") and key != "wad":
                result.sounds.add(low.removeprefix("sound/"))
    return result


def wad_names(value: str) -> list[str]:
    """The file names a worldspawn ``wad`` key lists (``\\half-life\\cstrike\\
    cs_dust.wad;…`` -> ``["cs_dust.wad"]``): the engine looks them up by name
    in the mod folder and the folders after it."""
    names = []
    for part in value.split(";"):
        name = part.strip().replace("\\", "/").rsplit("/", 1)[-1]
        if name and name.lower() not in (n.lower() for n in names):
            names.append(name)
    return names


__all__ = ["BSP_VERSION", "BspError", "DMODEL_SIZE", "LUMP_ENTITIES", "LUMP_MODELS",
           "MapResources", "entities", "read_map_resources", "wad_names"]
