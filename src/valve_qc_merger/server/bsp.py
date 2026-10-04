"""What a GoldSource map (BSP v30) precaches on its own.

Every brush model of the map takes a model slot ("*0" is the world itself,
"*1".. the doors, lifts, breakables...); entities name models and sprites
(``model`` / ``sprite`` keys) and sounds (``message`` of ambient_generic,
``noise*``...). Only values that are file names count; numeric sound
indices (doors) and sentences (``!name``) are the game DLL's own.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

BSP_VERSION = 30
LUMP_ENTITIES, LUMP_MODELS = 0, 14
DMODEL_SIZE = 64
_PAIR = re.compile(r'"([^"]*)"\s*"([^"]*)"')


class BspError(ValueError):
    """Not a readable BSP v30 map."""


@dataclass
class MapResources:
    name: str
    brush_models: int = 0  # world + submodels: each takes a model slot
    models: set[str] = field(default_factory=set)  # entity .mdl/.spr (game paths)
    sounds: set[str] = field(default_factory=set)  # entity .wav (relative to sound/)
    entities: int = 0

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
    data = Path(path).read_bytes()
    if len(data) < 4 + 15 * 8:
        raise BspError(f"{Path(path).name}: too short for a BSP")
    (version,) = struct.unpack_from("<i", data, 0)
    if version != BSP_VERSION:
        raise BspError(f"{Path(path).name}: BSP version {version} (GoldSource is 30)")
    lumps = [struct.unpack_from("<2i", data, 4 + i * 8) for i in range(15)]
    ent_off, ent_len = lumps[LUMP_ENTITIES]
    mod_off, mod_len = lumps[LUMP_MODELS]
    text = data[ent_off:ent_off + ent_len].split(b"\0", 1)[0].decode("latin-1")
    result = MapResources(name=Path(path).stem, brush_models=mod_len // DMODEL_SIZE)
    ents = entities(text)
    result.entities = len(ents)
    for entity in ents:
        for key, value in entity.items():
            low = value.lower().replace("\\", "/")
            if low.endswith((".mdl", ".spr")) and not low.startswith("*"):
                result.models.add(low)
            elif low.endswith(".wav") and not low.startswith("!") and key != "wad":
                result.sounds.add(low.removeprefix("sound/"))
    return result


__all__ = ["BspError", "MapResources", "entities", "read_map_resources"]
