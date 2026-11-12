"""The textures a map draws: embedded in the BSP or looked up in its WADs.

A texture of the textures lump with no pixels (all mip offsets 0) is
external: the engine looks its name up (case-insensitively) in the WADs the
worldspawn ``wad`` key lists, in that order, each found by file name in the
mod folder and the ones after it. :class:`WadIndex` reads only a WAD's
directory (and a texture's smallest mip level when its colour is asked), so
listing a map's sources is quick.

Name prefixes the engine reads: ``{`` transparent (index 255), ``!`` water,
``+0``…``+9`` an animation and ``+a``…``+j`` its alternate, ``-`` random
tiling, ``scroll`` a conveyor.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.geometry import MipTex, geometry

MIPTEX = 0x43
SPECIAL = ("sky", "aaatrigger", "clip", "null", "origin", "bevel", "hint", "skip",
           "black_hidden", "solidhint")
_ANIM = re.compile(r"^\+([0-9a-j])(.+)$", re.IGNORECASE)


@dataclass
class WadIndex:
    """A WAD3's miptex directory: lower-case name -> (offset, size)."""

    path: Path
    entries: dict[str, tuple[int, int]] = field(default_factory=dict)
    sizes: dict[str, tuple[int, int]] = field(default_factory=dict)  # name -> (w, h)
    error: str = ""

    @classmethod
    def read(cls, path: Path) -> WadIndex:
        index = cls(Path(path))
        try:
            with open(path, "rb") as handle:
                head = handle.read(12)
                if len(head) < 12 or head[:4] not in (b"WAD3", b"WAD2"):
                    index.error = "not a WAD3 file"
                    return index
                count, directory = struct.unpack_from("<2i", head, 4)
                handle.seek(directory)
                table = handle.read(count * 32)
                for i in range(len(table) // 32):
                    offset, disk, _size, kind, compression, _pad, raw = struct.unpack_from(
                        "<3iBBH16s", table, i * 32)
                    if kind != MIPTEX or compression:
                        continue
                    name = raw.split(b"\0", 1)[0].decode("latin-1").lower()
                    index.entries.setdefault(name, (offset, disk))
                for name, (offset, _disk) in index.entries.items():
                    handle.seek(offset + 16)
                    width, height = struct.unpack("<2I", handle.read(8))
                    index.sizes[name] = (width, height)
        except (OSError, struct.error) as exc:
            index.error = str(exc)
        return index

    def lump(self, name: str) -> bytes | None:
        entry = self.entries.get(name.lower())
        if entry is None:
            return None
        with open(self.path, "rb") as handle:
            handle.seek(entry[0])
            return handle.read(entry[1])


def miptex_colour(lump: bytes) -> tuple[int, int, int] | None:
    """The average colour of a miptex (its smallest mip level, the
    palette; transparent pixels of a ``{`` texture left out)."""
    try:
        raw, width, height, *offsets = struct.unpack_from("<16s2I4I", lump, 0)
        if not offsets[3]:
            return None
        small = np.frombuffer(lump, np.uint8, (width // 8) * (height // 8), offsets[3])
        end = offsets[3] + (width // 8) * (height // 8)
        (colours,) = struct.unpack_from("<H", lump, end)
        palette = np.frombuffer(lump, np.uint8, 3 * min(colours, 256), end + 2).reshape(-1, 3)
    except (struct.error, ValueError):
        return None
    if raw[:1] == b"{":
        small = small[small != 255]
    small = small[small < len(palette)]
    if not len(small):
        return None
    mean = palette[small].astype(np.float64).mean(axis=0)
    return int(mean[0]), int(mean[1]), int(mean[2])


@dataclass
class TextureUse:
    """A texture of the map: where it comes from and how much it is drawn."""

    index: int
    name: str
    width: int
    height: int
    embedded: bool
    wad: str = ""  # the WAD (file name) it is found in, for an external one
    faces: int = 0
    special: bool = False  # sky / trigger / clip …: no lightmap, not drawn as a surface

    @property
    def found(self) -> bool:
        return self.embedded or bool(self.wad)


def is_special(name: str) -> bool:
    low = name.lower()
    return low.startswith(SPECIAL) or low.startswith(("!", "water"))


def find_wads(names: list[str], folders: list[Path]) -> dict[str, Path | None]:
    """Each WAD file name -> its path in the first folder that has it
    (case-insensitively, as the Windows client finds it)."""
    listing: dict[str, Path] = {}
    for folder in folders:
        if folder.is_dir():
            for path in folder.iterdir():
                if path.suffix.lower() == ".wad":
                    listing.setdefault(path.name.lower(), path)
    return {name: listing.get(name.lower()) for name in names}


def texture_uses(bsp: B.Bsp, wads: dict[str, WadIndex]) -> list[TextureUse]:
    """Every texture of the map with its source (``wads``: file name ->
    index, in the worldspawn's order) and how many faces draw it."""
    geo = geometry(bsp)
    per_texture = (np.bincount(np.maximum(geo.face_texture(), 0), minlength=len(geo.textures))
                   if len(geo.faces) else np.zeros(len(geo.textures), np.int64))
    out = []
    for tex in geo.textures:
        if tex.offset < 0:
            continue
        use = TextureUse(tex.index, tex.name, tex.width, tex.height, tex.embedded,
                         faces=int(per_texture[tex.index]) if tex.index < len(per_texture) else 0,
                         special=is_special(tex.name))
        if not tex.embedded:
            use.wad = next((name for name, index in wads.items()
                            if tex.name.lower() in index.entries), "")
        out.append(use)
    return out


def broken_animations(names: list[str]) -> list[str]:
    """Animated textures with a gap (``+1name`` without ``+0name``, ``+b``
    without ``+a``): the engine stops with "Missing frame"."""
    frames: dict[tuple[str, bool], set[int]] = {}
    for name in names:
        match = _ANIM.match(name)
        if not match:
            continue
        key = match.group(1).lower()
        alternate = key.isalpha()
        number = ord(key) - (ord("a") if alternate else ord("0"))
        frames.setdefault((match.group(2).lower(), alternate), set()).add(number)
    out = []
    for (base, alternate), numbers in sorted(frames.items()):
        if numbers != set(range(max(numbers) + 1)):
            out.append(f"+{'a' if alternate else '0'}{base}")
    return out


def embedded_colour(bsp: B.Bsp, tex: MipTex) -> tuple[int, int, int] | None:
    if not tex.embedded:
        return None
    return miptex_colour(bsp.lumps[B.TEXTURES][tex.offset:])


__all__ = ["SPECIAL", "TextureUse", "WadIndex", "broken_animations", "embedded_colour",
           "find_wads", "is_special", "miptex_colour", "texture_uses"]
