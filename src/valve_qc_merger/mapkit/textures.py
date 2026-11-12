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
from valve_qc_merger.sprite.wad import Texture

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
    area: float = 0.0  # square units of the faces drawing it

    @property
    def prefixes(self) -> list[str]:
        """What the name's prefix tells the engine."""
        low = self.name.lower()
        out = []
        if low.startswith("{"):
            out.append("transparent")
        if low.startswith("!") or low.startswith("water"):
            out.append("water")
        if _ANIM.match(low):
            out.append("animated" + (" (alternate)" if low[1].isalpha() else ""))
        if low.startswith("-"):
            out.append("random tiling")
        if low.startswith("scroll"):
            out.append("conveyor")
        return out

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
    areas = (np.bincount(np.maximum(geo.face_texture(), 0), weights=geo.face_areas(),
                         minlength=len(geo.textures))
             if len(geo.faces) else np.zeros(len(geo.textures)))
    out = []
    for tex in geo.textures:
        if tex.offset < 0:
            continue
        use = TextureUse(tex.index, tex.name, tex.width, tex.height, tex.embedded,
                         faces=int(per_texture[tex.index]) if tex.index < len(per_texture) else 0,
                         special=is_special(tex.name),
                         area=float(areas[tex.index]) if tex.index < len(areas) else 0.0)
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




# -- changing textures ----------------------------------------------------------
#
# A texture keeps its size: its texture coordinates are texels (vecs), and the
# lightmaps are laid out from the same vecs — another size would tile it
# differently, scaling the vecs would move the light. A new picture is resized
# to the texture's own W x H.


def texture_from_image(name: str, rgba: np.ndarray, width: int, height: int) -> Texture:
    """A miptex texture ``name`` from an RGBA picture, resized to ``width`` x
    ``height``: 256 colours, or 255 + the key colour at index 255 where the
    picture is see-through for a ``{`` texture."""
    from valve_qc_merger.sprite.spr import quantize
    from valve_qc_merger.sprite.wad import KEY_COLOUR, MIPTEX, _resize

    image = _resize(rgba, width, height) if rgba.shape[:2] != (height, width) else rgba
    transparent = name.startswith("{")
    solid = image[..., 3] >= 128 if transparent else np.ones((height, width), bool)
    shown = image[..., :3][solid]
    count = 255 if transparent else 256
    palette, _ = quantize(shown[:, None, :] if len(shown) else np.zeros((1, 1, 3), np.uint8),
                          count)
    pal = palette.astype(np.int32)
    flat = image[..., :3].reshape(-1, 3).astype(np.int32)
    indices = np.empty(len(flat), np.uint8)
    for start in range(0, len(flat), 4096):
        chunk = flat[start:start + 4096]
        indices[start:start + 4096] = ((chunk[:, None, :] - pal[None]) ** 2).sum(2).argmin(1)
    grid = indices.reshape(height, width)
    colours = [tuple(int(c) for c in p) for p in palette]
    if transparent:
        grid[~solid] = 255
        colours += [(0, 0, 0)] * (255 - len(colours)) + [KEY_COLOUR]
    else:
        colours += [(0, 0, 0)] * (256 - len(colours))
    return Texture(name, width, height, grid.tobytes(), colours, MIPTEX)  # type: ignore[arg-type]


def external_entry(name: str, width: int, height: int) -> bytes:
    """A textures-lump entry with no pixels (found by name in the WADs)."""
    return struct.pack("<16s2I4I", name.encode("latin-1")[:15].ljust(16, b"\0"), width,
                       height, 0, 0, 0, 0)


def lump_entries(bsp: B.Bsp) -> list[bytes | None]:
    """Each texture's bytes in the textures lump as stored (None: an empty slot)."""
    lump = bsp.lumps[B.TEXTURES]
    table = geometry(bsp).textures
    offsets = sorted({t.offset for t in table if t.offset >= 0} | {len(lump)})
    out: list[bytes | None] = []
    for tex in table:
        if tex.offset < 0:
            out.append(None)
            continue
        end = offsets[offsets.index(tex.offset) + 1]
        out.append(lump[tex.offset:end] if tex.embedded else lump[tex.offset:tex.offset + 40])
    return out


def build_lump(entries: list[bytes | None]) -> bytes:
    """A textures lump of ``entries`` (each 4-byte aligned, in order)."""
    head = 4 + 4 * len(entries)
    body = bytearray()
    offsets = []
    for entry in entries:
        if entry is None:
            offsets.append(-1)
            continue
        while (head + len(body)) % 4:
            body += b"\0"
        offsets.append(head + len(body))
        body += entry
    return struct.pack(f"<i{len(entries)}i", len(entries), *offsets) + bytes(body)


def _index_of(bsp: B.Bsp, name: str) -> int:
    for tex in geometry(bsp).textures:
        if tex.offset >= 0 and tex.name.lower() == name.lower():
            return tex.index
    raise KeyError(f"the map has no texture {name}")


def replace_embedded(bsp: B.Bsp, name: str, texture: Texture) -> None:
    """Put ``texture``'s pixels into embedded texture ``name`` (same size)."""
    from valve_qc_merger.sprite.wad import miptex_lump

    index = _index_of(bsp, name)
    tex = geometry(bsp).textures[index]
    if (texture.width, texture.height) != (tex.width, tex.height):
        raise ValueError(f"{name} is {tex.width}x{tex.height}, the picture "
                         f"{texture.width}x{texture.height}")
    entries = lump_entries(bsp)
    texture.name = tex.name
    entries[index] = miptex_lump(texture)
    bsp.set_lump(B.TEXTURES, build_lump(entries))


def embed(bsp: B.Bsp, wads: list[WadIndex], names: list[str] | None = None) -> list[str]:
    """Copy external textures (all, or ``names``) from ``wads`` into the
    map. Returns the names embedded."""
    entries = lump_entries(bsp)
    wanted = {n.lower() for n in names} if names else None
    done = []
    for tex in geometry(bsp).textures:
        if tex.offset < 0 or tex.embedded or (wanted is not None
                                              and tex.name.lower() not in wanted):
            continue
        for wad in wads:
            lump = wad.lump(tex.name)
            if lump is not None:
                entries[tex.index] = tex.name.encode("latin-1")[:15].ljust(16, b"\0") + lump[16:]
                done.append(tex.name)
                break
    if done:
        bsp.set_lump(B.TEXTURES, build_lump(entries))
    return done


def extract(bsp: B.Bsp, names: list[str] | None = None, strip: bool = False) -> list[Texture]:
    """The embedded textures (all, or ``names``) as WAD textures; ``strip``
    leaves them external in the map (their pixels then come from a WAD)."""
    from valve_qc_merger.sprite.wad import read_miptex

    entries = lump_entries(bsp)
    wanted = {n.lower() for n in names} if names else None
    out = []
    for tex in geometry(bsp).textures:
        entry = entries[tex.index] if tex.offset >= 0 else None
        if entry is None or not tex.embedded or (wanted is not None
                                                  and tex.name.lower() not in wanted):
            continue
        out.append(read_miptex(entry, tex.name, MIPTEX))
        if strip:
            entries[tex.index] = external_entry(tex.name, tex.width, tex.height)
    if strip and out:
        bsp.set_lump(B.TEXTURES, build_lump(entries))
    return out


def pixel_hash(texture: Texture) -> str:
    import hashlib
    digest = hashlib.sha1(texture.indices)
    digest.update(bytes(int(c) for rgb in texture.palette for c in rgb))
    return digest.hexdigest()


def wad_of_lumps(lumps: list[tuple[str, bytes]]) -> bytes:
    """A WAD3 of miptex lumps written as they are (no mip levels redone)."""
    directory = 12 + sum(len(lump) for _n, lump in lumps)
    out = [struct.pack("<4s2i", b"WAD3", len(lumps), directory)]
    entries, position = [], 12
    for name, lump in lumps:
        raw = name.encode("latin-1")[:15].ljust(16, b"\0")
        entries.append(struct.pack("<3iBBH16s", position, len(lump), len(lump), MIPTEX, 0, 0,
                                   raw))
        out.append(lump)
        position += len(lump)
    return b"".join(out + entries)


@dataclass
class SharedWad:
    textures: list[Texture] = field(default_factory=list)
    lumps: list[tuple[str, bytes]] = field(default_factory=list)  # as found, for the WAD
    used_by: dict[str, list[str]] = field(default_factory=dict)  # texture -> maps
    conflicts: list[str] = field(default_factory=list)  # one name, other pixels
    missing: list[str] = field(default_factory=list)  # "map: texture" found nowhere


def shared_wad(maps: list[tuple[B.Bsp, list[WadIndex]]]) -> SharedWad:
    """One WAD for a pack of maps: every texture they draw (embedded or
    from their WADs) once — the same pixels under one name are kept once,
    one name with other pixels is a conflict (the first map's is kept; tool
    textures — sky, clip, null … — are not drawn and never conflict)."""
    from valve_qc_merger.sprite.wad import read_miptex

    result = SharedWad()
    seen: dict[str, str] = {}
    for bsp, wads in maps:
        entries = lump_entries(bsp)
        for tex in geometry(bsp).textures:
            if tex.offset < 0 or not tex.name:
                continue
            texture, raw = None, b""
            if tex.embedded and entries[tex.index] is not None:
                raw = entries[tex.index] or b""
                texture = read_miptex(raw, tex.name, MIPTEX)
            else:
                for wad in wads:
                    lump = wad.lump(tex.name)
                    if lump is not None:
                        raw = lump
                        texture = read_miptex(lump, tex.name, MIPTEX)
                        break
            if texture is None:
                if not tex.name.lower().startswith(SPECIAL):
                    result.missing.append(f"{bsp.name}: {tex.name}")
                continue
            key = tex.name.lower()
            result.used_by.setdefault(tex.name, [])
            if bsp.name not in result.used_by[tex.name]:
                result.used_by[tex.name].append(bsp.name)
            digest = pixel_hash(texture)
            if key in seen:
                if seen[key] != digest and tex.name not in result.conflicts \
                        and not key.startswith(SPECIAL):
                    result.conflicts.append(tex.name)
                continue
            seen[key] = digest
            result.textures.append(texture)
            result.lumps.append((tex.name, raw))
    return result


__all__ = ["SPECIAL", "SharedWad", "TextureUse", "WadIndex", "broken_animations", "build_lump",
           "embed", "embedded_colour", "external_entry", "extract", "find_wads", "is_special",
           "lump_entries", "miptex_colour", "pixel_hash", "replace_embedded", "shared_wad",
           "texture_from_image", "texture_uses", "wad_of_lumps"]
