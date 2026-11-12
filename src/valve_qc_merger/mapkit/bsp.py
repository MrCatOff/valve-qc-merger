"""A GoldSource map (BSP v30), read and written lump by lump.

The file is a version (30) and 15 lumps (offset, length). :class:`Bsp`
keeps every lump's raw bytes; the parsed views (:mod:`.geometry`) are
built from them on demand, and :meth:`Bsp.write` lays the lumps out again:
nothing changed -> the very same bytes; a lump of a new size -> the lumps
after it move (4-byte aligned), every other lump is copied byte for byte.

The engine's map CRC (``CRC_MapFile``: the one a client compares with the
server's, "map differs from the server's") runs over every lump but the
entities in lump order, so an entity edit keeps it and clients with the
original map still join. :meth:`Bsp.crc` computes it.

Blue Shift maps swap the first two lumps (planes first, then entities):
:attr:`Bsp.blue_shift` tells them; Counter-Strike does not load them.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

VERSION = 30
LUMP_NAMES = ("entities", "planes", "textures", "vertexes", "visibility", "nodes", "texinfo",
              "faces", "lighting", "clipnodes", "leafs", "marksurfaces", "edges", "surfedges",
              "models")
LUMPS = len(LUMP_NAMES)
HEADER = 4 + LUMPS * 8
(ENTITIES, PLANES, TEXTURES, VERTEXES, VISIBILITY, NODES, TEXINFO, FACES, LIGHTING,
 CLIPNODES, LEAFS, MARKSURFACES, EDGES, SURFEDGES, MODELS) = range(LUMPS)
RECORD = {PLANES: 20, VERTEXES: 12, NODES: 24, TEXINFO: 40, FACES: 20, CLIPNODES: 8,
          LEAFS: 28, MARKSURFACES: 2, EDGES: 4, SURFEDGES: 4, MODELS: 64}


class BspError(ValueError):
    """Not a readable BSP v30 map."""


@dataclass
class Bsp:
    """One map: its lumps as bytes (``lumps[i]``) and where they were."""

    name: str
    lumps: list[bytes]
    offsets: list[int] = field(default_factory=list)  # as read (layout order)
    source: bytes = b""  # the file as read: an unchanged map writes it back
    blue_shift: bool = False
    _cache: dict[str, object] = field(default_factory=dict, repr=False)

    @classmethod
    def read(cls, path: Path | str) -> Bsp:
        path = Path(path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise BspError(f"{path.name}: {exc}") from exc
        return cls.parse(data, path.stem)

    @classmethod
    def parse(cls, data: bytes, name: str = "map") -> Bsp:
        if len(data) < HEADER:
            raise BspError(f"{name}: too short for a BSP")
        (version,) = struct.unpack_from("<i", data, 0)
        if version != VERSION:
            raise BspError(f"{name}: BSP version {version} (GoldSource is 30)")
        table = [struct.unpack_from("<2i", data, 4 + i * 8) for i in range(LUMPS)]
        for index, (offset, length) in enumerate(table):
            if offset < 0 or length < 0 or offset + length > len(data):
                raise BspError(f"{name}: lump {LUMP_NAMES[index]} lies outside the file")
        lumps = [data[o:o + n] for o, n in table]
        blue = _looks_like_entities(lumps[PLANES]) and not _looks_like_entities(lumps[ENTITIES])
        if blue:
            lumps[ENTITIES], lumps[PLANES] = lumps[PLANES], lumps[ENTITIES]
        return cls(name, lumps, [o for o, _n in table], data, blue)

    # -- entities -----------------------------------------------------------

    @property
    def entity_text(self) -> str:
        return self.lumps[ENTITIES].split(b"\0", 1)[0].decode("latin-1")

    def set_entity_text(self, text: str) -> None:
        self.set_lump(ENTITIES, text.encode("latin-1") + b"\0")

    # -- lumps --------------------------------------------------------------

    def set_lump(self, index: int, data: bytes) -> None:
        self.lumps[index] = bytes(data)
        self._cache.clear()

    def count(self, index: int) -> int:
        """Records in a fixed-size lump."""
        return len(self.lumps[index]) // RECORD[index]

    def changed(self) -> list[int]:
        """Lumps that differ from the file as read."""
        if not self.source:
            return list(range(LUMPS))
        original = Bsp.parse(self.source, self.name)
        return [i for i in range(LUMPS) if original.lumps[i] != self.lumps[i]]

    def crc(self) -> int:
        """The engine's map CRC: CRC-32 of every lump but the entities."""
        value = 0
        for index in range(LUMPS):
            if index != ENTITIES:
                value = zlib.crc32(self.lumps[index], value)
        return value & 0xFFFFFFFF

    def write(self) -> bytes:
        """The map as bytes: the file as read when nothing changed; else the
        lumps in their original file order — the ones before the first
        changed lump where they were, the rest moved up or down, 4-byte
        aligned. A Blue Shift map is written as a standard one."""
        if self.source and not self.blue_shift and not self.changed():
            return self.source
        order = sorted(range(LUMPS), key=lambda i: (self.offsets[i] if self.offsets else i, i))
        original = Bsp.parse(self.source, self.name) if self.source else None
        body = bytearray(self.source[:HEADER] if self.source else bytes(HEADER))
        table = [(0, 0)] * LUMPS
        moved = original is None or self.blue_shift
        for index in order:
            data = self.lumps[index]
            if not moved and original is not None and original.lumps[index] == data:
                start = self.offsets[index]
                if start >= len(body):
                    body += self.source[len(body):start]  # the gap as it was
                    table[index] = (start, len(data))
                    body += data
                    continue
            moved = True
            while len(body) % 4:
                body += b"\0"
            table[index] = (len(body), len(data))
            body += data
        header = struct.pack("<i", VERSION) + b"".join(struct.pack("<2i", *t) for t in table)
        body[:HEADER] = header
        return bytes(body)

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.write_bytes(self.write())
        return path


def _looks_like_entities(data: bytes) -> bool:
    return data.lstrip(b" \t\r\n")[:1] == b"{"


def map_crc(path: Path | str) -> int:
    return Bsp.read(path).crc()


__all__ = ["Bsp", "BspError", "CLIPNODES", "EDGES", "ENTITIES", "FACES", "HEADER", "LEAFS",
           "LIGHTING", "LUMPS", "LUMP_NAMES", "MARKSURFACES", "MODELS", "NODES", "PLANES",
           "RECORD", "SURFEDGES", "TEXINFO", "TEXTURES", "VERSION", "VERTEXES", "VISIBILITY",
           "map_crc"]
