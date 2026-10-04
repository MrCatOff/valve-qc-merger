"""Edit the entities of a GoldSource map: remove, change, replace.

The entity lump is plain text (``{ "key" "value" … }`` blocks). Two ways to
put an edited list on a server:

- a ``maps/<map>.ent`` file — ReHLDS reads it instead of the lump with
  ``sv_use_entity_file 1``; the BSP stays untouched;
- a rewritten BSP — the other lumps are copied byte for byte, so the map's
  CRC (which the engine computes without the entity lump) does not change
  and clients with the original map still join.

A typical ZM edit: dropped-weapon spawns (``armoury_entity``) become a model
of your own (``cycler_sprite`` with ``models/w_supplybox.mdl``) or bare
markers (``info_target`` named ``vqm_supplybox``) a plugin spawns supply
boxes at.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

from valve_qc_merger.server.bsp import BSP_VERSION, LUMP_ENTITIES, BspError

LUMPS = 15
HEADER = 4 + LUMPS * 8
_BLOCK = re.compile(r"\{([^{}]*)\}")
_PAIR = re.compile(r'"([^"]*)"\s*"([^"]*)"')
KEEP_PLACE = ("origin", "angles")


@dataclass
class MapEntities:
    path: Path
    entities: list[dict[str, str]]

    def classes(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entity in self.entities:
            name = entity.get("classname", "")
            counts[name] = counts.get(name, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def parse(text: str) -> list[dict[str, str]]:
    return [dict(_PAIR.findall(block)) for block in _BLOCK.findall(text)]


def entity_text(entities: list[dict[str, str]]) -> str:
    """The lump / ``.ent`` text of ``entities``."""
    out = []
    for entity in entities:
        out.append("{")
        out += [f'"{key}" "{value}"' for key, value in entity.items()]
        out.append("}")
    return "\n".join(out) + "\n"


def _lumps(data: bytes, name: str) -> list[tuple[int, int]]:
    if len(data) < HEADER:
        raise BspError(f"{name}: too short for a BSP")
    (version,) = struct.unpack_from("<i", data, 0)
    if version != BSP_VERSION:
        raise BspError(f"{name}: BSP version {version} (GoldSource is 30)")
    lumps = [struct.unpack_from("<2i", data, 4 + i * 8) for i in range(LUMPS)]
    for offset, length in lumps:
        if offset < 0 or length < 0 or offset + length > len(data):
            raise BspError(f"{name}: a lump lies outside the file")
    return lumps


def read_entities(path: Path) -> MapEntities:
    path = Path(path)
    data = path.read_bytes()
    offset, length = _lumps(data, path.name)[LUMP_ENTITIES]
    text = data[offset:offset + length].split(b"\0", 1)[0].decode("latin-1")
    return MapEntities(path, parse(text))


def read_ent_file(path: Path) -> list[dict[str, str]]:
    return parse(Path(path).read_text(encoding="latin-1"))


def write_ent_file(entities: list[dict[str, str]], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(entity_text(entities).encode("latin-1"))
    return path


def with_entities(data: bytes, entities: list[dict[str, str]], name: str = "map") -> bytes:
    """The BSP ``data`` with its entity lump replaced: every other lump is
    copied unchanged (in its original order, 4-byte aligned)."""
    lumps = _lumps(data, name)
    blobs = [data[o:o + n] for o, n in lumps]
    blobs[LUMP_ENTITIES] = entity_text(entities).encode("latin-1") + b"\0"
    order = sorted(range(LUMPS), key=lambda i: (lumps[i][0], i))
    if LUMP_ENTITIES not in order:  # pragma: no cover - always present
        order.append(LUMP_ENTITIES)
    body = bytearray()
    table = [(0, 0)] * LUMPS
    for index in order:
        while (HEADER + len(body)) % 4:
            body += b"\0"
        table[index] = (HEADER + len(body), len(blobs[index]))
        body += blobs[index]
    header = struct.pack("<i", BSP_VERSION) + b"".join(struct.pack("<2i", *t) for t in table)
    return header + bytes(body)


def save_bsp(source: Path, entities: list[dict[str, str]], target: Path | None = None
             ) -> tuple[Path, Path | None]:
    """Write the map with ``entities`` to ``target`` (default: over
    ``source``, which is kept as ``<map>.bsp.bak`` first)."""
    source = Path(source)
    target = Path(target) if target else source
    data = source.read_bytes()
    backup = None
    if target.resolve() == source.resolve():
        backup = source.with_name(source.name + ".bak")
        if not backup.exists():
            backup.write_bytes(data)
    target.write_bytes(with_entities(data, entities, source.name))
    return target, backup


def replace(entity: dict[str, str], classname: str, extra: dict[str, str]) -> dict[str, str]:
    """A new entity of ``classname`` at ``entity``'s place (origin, angles)
    with the keys of ``extra``."""
    out = {"classname": classname}
    out.update({k: entity[k] for k in KEEP_PLACE if k in entity})
    out.update({k: v for k, v in extra.items() if v != ""})
    return out


def as_model(entity: dict[str, str], model: str) -> dict[str, str]:
    """A static model in the entity's place (``cycler_sprite`` draws a .mdl
    or .spr, takes no damage, blocks nothing)."""
    return replace(entity, "cycler_sprite", {"model": model, "framerate": "1"})


def as_marker(entity: dict[str, str], targetname: str) -> dict[str, str]:
    """A bare point in the entity's place for a plugin to find by name."""
    return replace(entity, "info_target", {"targetname": targetname})


def models_of(entities: list[dict[str, str]]) -> set[str]:
    """The .mdl/.spr files entities name (they take model slots)."""
    out = set()
    for entity in entities:
        for value in entity.values():
            low = value.lower().replace("\\", "/")
            if low.endswith((".mdl", ".spr")) and not low.startswith("*"):
                out.add(low)
    return out


__all__ = ["KEEP_PLACE", "MapEntities", "as_marker", "as_model", "entity_text", "models_of",
           "parse", "read_ent_file", "read_entities", "replace", "save_bsp",
           "with_entities", "write_ent_file"]
