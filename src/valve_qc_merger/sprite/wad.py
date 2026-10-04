"""WAD3 texture archives: map textures, ``decals.wad`` and spray logos.

A WAD3 is a header (``WAD3``, entry count, directory offset), the lumps and
a directory of 32-byte entries (offset, disk size, size, type, compression,
16-byte name). Textures are *miptex*: name, width, height, four offsets to
the full image and three halved mip levels (8-bit indices), then a 256-colour
palette. Map and decal textures use lump type ``0x43``; the spray file a
player's client sends (``tempdecal.wad``) holds one ``{LOGO`` texture of
type ``0x40``. A ``{`` name makes palette index 255 transparent.

Spray limits: both sides a multiple of 16, neither over 256 and at most
14336 pixels (112 x 128 and the like; the client refuses bigger). Stock HLDS
refuses logos over 64 x 64; ReHLDS takes them with
``sv_rehlds_allow_large_sprays 1`` (its default).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from valve_qc_merger.sprite.spr import quantize

MIPTEX, SPRAY = 0x43, 0x40
SPRAY_NAME = "{LOGO"
SPRAY_MAX_AREA = 14336
SPRAY_MAX_SIDE = 256
KEY_COLOUR = (0, 0, 255)


class WadError(ValueError):
    """Not a readable WAD3."""


@dataclass
class Texture:
    name: str
    width: int
    height: int
    indices: bytes  # width * height palette indices (the full-size level)
    palette: list[tuple[int, int, int]]
    lump_type: int = MIPTEX

    @property
    def transparent(self) -> bool:
        return self.name.startswith("{")

    @property
    def alpha_decal(self) -> bool:
        """A ``decals.wad`` decal: palette 0..254 a grey ramp from white down,
        the index is the opacity and palette[255] the colour."""
        if not self.transparent or len(self.palette) < 256:
            return False
        ramp = np.array(self.palette[:255], np.int32)
        return bool((ramp[:, 0] == ramp[:, 1]).all() and (ramp[:, 1] == ramp[:, 2]).all()
                    and (np.diff(ramp[:, 0]) <= 0).all() and ramp[0, 0] > ramp[-1, 0])

    def rgba(self) -> np.ndarray:
        """(h, w, 4) uint8; index 255 transparent for ``{`` textures (the
        index is the opacity for an :attr:`alpha_decal`)."""
        idx = np.frombuffer(self.indices, np.uint8).reshape(self.height, self.width)
        if self.alpha_decal:
            out = np.empty((self.height, self.width, 4), np.uint8)
            out[..., :3] = self.palette[255]
            out[..., 3] = idx
            return out
        pal = np.zeros((256, 4), np.uint8)
        pal[:len(self.palette), :3] = np.array(self.palette, np.uint8).reshape(-1, 3)
        pal[:, 3] = 255
        if self.transparent:
            pal[255, 3] = 0
        return pal[idx]


def read_wad(source: Path | bytes) -> list[Texture]:
    """The miptex lumps of a WAD3 (other lump kinds — fonts, qpics — skipped)."""
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    if len(data) < 12 or data[:4] != b"WAD3":
        raise WadError("not a WAD3 file")
    count, directory = struct.unpack_from("<2i", data, 4)
    if count < 0 or directory < 12 or directory + count * 32 > len(data):
        raise WadError("broken WAD3 directory")
    out = []
    for i in range(count):
        offset, disk, _size, kind, compression, _pad, raw = struct.unpack_from(
            "<3iBBH16s", data, directory + i * 32)
        if kind not in (MIPTEX, SPRAY) or compression:
            continue
        lump = data[offset:offset + disk]
        name = raw.split(b"\0", 1)[0].decode("latin-1")
        try:
            out.append(_read_miptex(lump, name, kind))
        except (struct.error, ValueError) as exc:
            raise WadError(f"texture {name!r}: {exc}") from exc
    return out


def _read_miptex(lump: bytes, name: str, kind: int) -> Texture:
    _name, width, height, *offsets = struct.unpack_from("<16s2I4I", lump, 0)
    if not 0 < width <= 4096 or not 0 < height <= 4096:
        raise ValueError(f"size {width}x{height}")
    pixels = lump[offsets[0]:offsets[0] + width * height]
    end = offsets[3] + (width // 8) * (height // 8)
    (colours,) = struct.unpack_from("<H", lump, end)
    raw = lump[end + 2:end + 2 + 3 * colours]
    palette = [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]
    if len(pixels) != width * height:
        raise ValueError("truncated pixels")
    return Texture(name, width, height, pixels, palette, kind)  # type: ignore[arg-type]


def _mip(indices: np.ndarray, palette: np.ndarray, level: int, transparent: bool
         ) -> np.ndarray:
    """A level halved ``level`` times: averaged colour, nearest palette entry
    (a transparent texture keeps 255 where most of the block is clear)."""
    step = 1 << level
    height, width = indices.shape[0] // step, indices.shape[1] // step
    blocks = indices[:height * step, :width * step].reshape(height, step, width, step)
    colours = palette[blocks].astype(np.float64)  # (h, s, w, s, 3)
    mean = colours.mean(axis=(1, 3))
    usable = palette[:255] if transparent else palette
    flat = mean.reshape(-1, 3)
    out = ((flat[:, None, :] - usable[None].astype(np.float64)) ** 2).sum(2).argmin(1)
    out = out.reshape(height, width).astype(np.uint8)
    if transparent:
        clear = (blocks == 255).mean(axis=(1, 3)) >= 0.5
        out[clear] = 255
    return out


def _miptex(texture: Texture) -> bytes:
    width, height = texture.width, texture.height
    palette = np.zeros((256, 3), np.uint8)
    palette[:len(texture.palette)] = np.array(texture.palette, np.uint8).reshape(-1, 3)
    full = np.frombuffer(texture.indices, np.uint8).reshape(height, width)
    levels = [full] + [_mip(full, palette, level, texture.transparent) for level in (1, 2, 3)]
    offsets, body, position = [], b"", 40
    for level in levels:
        offsets.append(position)
        body += level.tobytes()
        position += level.size
    name = texture.name.encode("latin-1")[:15].ljust(16, b"\0")
    return (struct.pack("<16s2I4I", name, width, height, *offsets) + body
            + struct.pack("<H", 256) + palette.tobytes() + b"\0\0")


def write_wad(textures: list[Texture]) -> bytes:
    """A WAD3 of ``textures`` (each lump keeps its type)."""
    lumps = [_miptex(t) for t in textures]
    directory = 12 + sum(len(lump) for lump in lumps)
    out = [struct.pack("<4s2i", b"WAD3", len(lumps), directory)]
    entries, position = [], 12
    for texture, lump in zip(textures, lumps, strict=True):
        name = texture.name.encode("latin-1")[:15].ljust(16, b"\0")
        entries.append(struct.pack("<3iBBH16s", position, len(lump), len(lump),
                                   texture.lump_type, 0, 0, name))
        out.append(lump)
        position += len(lump)
    return b"".join(out + entries)


# --------------------------------------------------------------------------- #
# sprays
# --------------------------------------------------------------------------- #
def spray_size(width: int, height: int, *, max_area: int = SPRAY_MAX_AREA,
               max_side: int = SPRAY_MAX_SIDE, upscale: bool = False) -> tuple[int, int]:
    """The largest spray size with ``width:height``'s aspect: sides multiples
    of 16, each at most ``max_side``, area at most ``max_area`` (and, unless
    ``upscale``, not much over the image's own)."""
    if width <= 0 or height <= 0:
        raise ValueError("empty image")
    if not upscale:
        max_area = min(max_area, max(256, width * height))
    aspect = width / height
    best = (16, 16)
    for w in range(16, max_side + 1, 16):
        h = max(16, min(max_side, int(round(w / aspect / 16)) * 16))
        if w * h > max_area:
            continue
        error = abs(np.log((w / h) / aspect))
        current = abs(np.log((best[0] / best[1]) / aspect))
        if (error < current - 0.02) or (abs(error - current) <= 0.02
                                        and w * h > best[0] * best[1]):
            best = (w, h)
    return best


def _resize(rgba: np.ndarray, width: int, height: int) -> np.ndarray:
    """Area-average resize (premultiplied alpha) of an (h, w, 4) uint8 image."""
    src_h, src_w = rgba.shape[:2]
    image = rgba.astype(np.float64)
    image[..., :3] *= image[..., 3:4] / 255.0

    def axis_weights(src: int, dst: int) -> np.ndarray:
        weights = np.zeros((dst, src))
        scale = src / dst
        for i in range(dst):
            lo, hi = i * scale, (i + 1) * scale
            for j in range(int(np.floor(lo)), min(int(np.ceil(hi)), src)):
                weights[i, j] = min(hi, j + 1) - max(lo, j)
        return weights / weights.sum(axis=1, keepdims=True)

    rows, cols = axis_weights(src_h, height), axis_weights(src_w, width)
    out = np.tensordot(rows, image, axes=(1, 0))  # (height, src_w, 4)
    out = np.tensordot(out, cols, axes=(1, 1)).transpose(0, 2, 1)  # (height, width, 4)
    alpha = out[..., 3:4]
    out[..., :3] = np.where(alpha > 0, out[..., :3] * 255.0 / np.maximum(alpha, 1e-9), 0)
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def make_spray(rgba: np.ndarray, size: tuple[int, int] | None = None) -> Texture:
    """A ``{LOGO`` texture from an (h, w, 4) uint8 image: resized to ``size``
    (default: :func:`spray_size`), 255 colours, see-through where the image
    is (alpha < 128) as palette index 255."""
    width, height = size or spray_size(rgba.shape[1], rgba.shape[0])
    if width % 16 or height % 16 or width * height > SPRAY_MAX_AREA \
            or max(width, height) > SPRAY_MAX_SIDE:
        raise ValueError(f"{width}x{height} is not a valid spray size")
    image = _resize(rgba, width, height)
    solid = image[..., 3] >= 128
    shown = image[..., :3][solid]
    palette, _ = quantize(shown[:, None, :] if len(shown) else
                          np.zeros((1, 1, 3), np.uint8), 255)
    pal = palette.astype(np.int32)
    flat = image[..., :3].reshape(-1, 3).astype(np.int32)
    indices = np.empty(len(flat), np.uint8)
    for start in range(0, len(flat), 4096):
        chunk = flat[start:start + 4096]
        indices[start:start + 4096] = ((chunk[:, None, :] - pal[None]) ** 2).sum(2).argmin(1)
    indices = indices.reshape(height, width)
    indices[~solid] = 255
    colours = [tuple(int(c) for c in p) for p in palette]
    colours += [(0, 0, 0)] * (255 - len(colours)) + [KEY_COLOUR]
    return Texture(SPRAY_NAME, width, height, indices.tobytes(), colours, SPRAY)


__all__ = ["KEY_COLOUR", "MIPTEX", "SPRAY", "SPRAY_MAX_AREA", "SPRAY_NAME", "Texture",
           "WadError", "make_spray", "read_wad", "spray_size", "write_wad"]
