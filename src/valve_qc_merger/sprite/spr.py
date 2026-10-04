"""GoldSource sprites (``IDSP`` version 2): read, write, make from images.

A sprite is up to N frames of 8-bit palette indices sharing one 256-colour
palette. The *type* sets how it faces the viewer (``parallel`` for HUD and
most effects); the *texture format* how colours are used:

- ``normal`` — opaque palette colours;
- ``additive`` — added to the scene (glows, muzzle flashes): black is
  invisible;
- ``indexalpha`` — the index IS the opacity, every pixel has palette[255]'s
  colour (smoke, decals);
- ``alphatest`` — index 255 is a hole (cut-out icons; HUD sheets).

Images become sprites through :func:`quantize`: exact when they use at most
the colours the format allows, else median cut.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

TYPES = ("parallel_upright", "facing_upright", "parallel", "oriented",
         "parallel_oriented")
FORMATS = ("normal", "additive", "indexalpha", "alphatest")
# 40 bytes: ident, version, type, format, radius, width, height, frames, beam, sync
HEADER = struct.Struct("<4s3if3ifi")


class SprError(ValueError):
    """Not a readable GoldSource sprite."""


@dataclass
class Frame:
    width: int
    height: int
    pixels: bytes  # width * height indices, top-down
    origin: tuple[int, int] = (0, 0)  # (x, y) of the top-left from the centre
    interval: float | None = None  # inside a frame group


@dataclass
class Sprite:
    type: int = 2  # parallel
    format: int = 0  # normal
    palette: list[tuple[int, int, int]] = field(
        default_factory=lambda: [(0, 0, 0)] * 256)
    frames: list[Frame] = field(default_factory=list)
    beam_length: float = 0.0
    sync: int = 0

    @property
    def size(self) -> tuple[int, int]:
        return (max((f.width for f in self.frames), default=0),
                max((f.height for f in self.frames), default=0))

    @property
    def type_name(self) -> str:
        return TYPES[self.type] if 0 <= self.type < len(TYPES) else str(self.type)

    @property
    def format_name(self) -> str:
        return FORMATS[self.format] if 0 <= self.format < len(FORMATS) else str(self.format)

    def rgba(self, index: int = 0) -> np.ndarray:
        """Frame ``index`` as (h, w, 4) uint8, the format applied."""
        frame = self.frames[index]
        idx = np.frombuffer(frame.pixels, np.uint8).reshape(frame.height, frame.width)
        pal = np.array(self.palette, np.uint8)
        rgb = pal[idx]
        alpha = np.full(idx.shape, 255, np.uint8)
        if self.format == 3:  # alphatest
            alpha[idx == 255] = 0
        elif self.format == 2:  # indexalpha
            rgb = np.broadcast_to(pal[255], rgb.shape).copy()
            alpha = idx.copy()
        elif self.format == 1:  # additive: brightness is opacity on a dark scene
            alpha = rgb.max(axis=2)
        return np.dstack([rgb, alpha])


def read_spr(source: Path | bytes) -> Sprite:
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    if len(data) < HEADER.size + 2 or data[:4] != b"IDSP":
        raise SprError("not a GoldSource sprite (IDSP)")
    (_ident, version, stype, fmt, _radius, _w, _h, numframes, beam,
     sync) = HEADER.unpack_from(data, 0)
    if version != 2:
        raise SprError(f"sprite version {version} (GoldSource is 2)")
    cursor = HEADER.size
    (colours,) = struct.unpack_from("<h", data, cursor)
    cursor += 2
    raw = data[cursor:cursor + colours * 3]
    palette = [tuple(raw[i * 3:i * 3 + 3]) for i in range(colours)]
    palette += [(0, 0, 0)] * (256 - len(palette))
    cursor += colours * 3
    sprite = Sprite(stype, fmt, palette, [], beam, sync)

    def single(at: int, interval: float | None = None) -> int:
        ox, oy, width, height = struct.unpack_from("<4i", data, at)
        at += 16
        sprite.frames.append(Frame(width, height, data[at:at + width * height], (ox, oy),
                                   interval))
        return at + width * height

    try:
        for _ in range(numframes):
            (kind,) = struct.unpack_from("<i", data, cursor)
            cursor += 4
            if kind == 0:
                cursor = single(cursor)
            else:
                (count,) = struct.unpack_from("<i", data, cursor)
                intervals = struct.unpack_from(f"<{count}f", data, cursor + 4)
                cursor += 4 + 4 * count
                for k in range(count):
                    cursor = single(cursor, intervals[k])
    except struct.error as exc:
        raise SprError(f"truncated sprite ({exc})") from exc
    return sprite


def write_spr(sprite: Sprite) -> bytes:
    width, height = sprite.size
    radius = float(np.hypot(width / 2, height / 2))
    out = bytearray(HEADER.pack(b"IDSP", 2, sprite.type, sprite.format, radius, width,
                                height, len(sprite.frames), sprite.beam_length, sprite.sync))
    out += struct.pack("<h", 256)
    out += bytes(c for colour in sprite.palette[:256] for c in colour)
    for frame in sprite.frames:
        ox, oy = frame.origin if frame.origin != (0, 0) else (-(frame.width // 2),
                                                             frame.height // 2)
        out += struct.pack("<i4i", 0, ox, oy, frame.width, frame.height)
        out += frame.pixels
    return bytes(out)


# --------------------------------------------------------------------------- #
# images -> palette
# --------------------------------------------------------------------------- #
def _median_cut(colours: np.ndarray, count: int) -> np.ndarray:
    """``count`` representative colours of (N, 3) uint8 ``colours``."""
    boxes = [colours.astype(np.int32)]
    while len(boxes) < count:
        index = max(range(len(boxes)), key=lambda i: (np.ptp(boxes[i], axis=0).max()
                                                       if len(boxes[i]) > 1 else -1))
        box = boxes[index]
        if len(box) < 2:
            break
        axis = int(np.ptp(box, axis=0).argmax())
        box = box[box[:, axis].argsort()]
        half = len(box) // 2
        boxes[index:index + 1] = [box[:half], box[half:]]
    return np.array([b.mean(axis=0).round() for b in boxes if len(b)], np.uint8)


def quantize(rgb: np.ndarray, count: int) -> tuple[np.ndarray, np.ndarray]:
    """(palette (k, 3), indices (h, w)) with k <= ``count``."""
    flat = rgb.reshape(-1, 3)
    unique, inverse = np.unique(flat, axis=0, return_inverse=True)
    if len(unique) <= count:
        return unique.astype(np.uint8), inverse.reshape(rgb.shape[:2]).astype(np.uint8)
    sample = unique if len(unique) <= 65536 else unique[
        np.random.default_rng(0).choice(len(unique), 65536, replace=False)]
    palette = _median_cut(sample, count)
    # nearest palette entry per unique colour, then per pixel
    nearest = np.empty(len(unique), np.int64)
    pal = palette.astype(np.int32)
    for start in range(0, len(unique), 4096):
        chunk = unique[start:start + 4096].astype(np.int32)
        nearest[start:start + 4096] = ((chunk[:, None, :] - pal[None]) ** 2).sum(2).argmin(1)
    return palette, nearest[inverse].reshape(rgb.shape[:2]).astype(np.uint8)


def from_images(images: list[np.ndarray], *, fmt: str = "alphatest",
                stype: str = "parallel", key_colour: tuple[int, int, int] = (0, 0, 255),
                tint: tuple[int, int, int] | None = None) -> Sprite:
    """A sprite whose frames are ``images`` ((h, w, 4) uint8 RGBA, one
    shared palette). ``key_colour`` fills palette[255] for alphatest holes;
    ``tint`` is the colour of an indexalpha sprite (default: the images'
    average)."""
    fmt_index, type_index = FORMATS.index(fmt), TYPES.index(stype)
    sprite = Sprite(type=type_index, format=fmt_index)
    if fmt == "indexalpha":
        colours = np.concatenate([im[..., :3][im[..., 3] > 0] for im in images] or
                                 [np.zeros((1, 3), np.uint8)])
        colour = tint or tuple(int(c) for c in (colours.mean(axis=0) if len(colours)
                                                else (255, 255, 255)))
        sprite.palette = [(i, i, i) for i in range(255)] + [tuple(colour)]
        for im in images:
            sprite.frames.append(Frame(im.shape[1], im.shape[0], im[..., 3].tobytes()))
        return sprite
    stacked = []
    for im in images:
        rgb = im[..., :3].astype(np.float64)
        alpha = im[..., 3:4].astype(np.float64) / 255.0
        if fmt == "additive":  # transparency becomes darkness
            rgb = rgb * alpha
        stacked.append(rgb.round().astype(np.uint8))
    allowed = 255 if fmt == "alphatest" else 256
    # alphatest holes say nothing about colour: the palette comes from what shows
    parts = [s[im[..., 3] >= 128] if fmt == "alphatest" else s.reshape(-1, 3)
             for im, s in zip(images, stacked, strict=True)]
    joined = np.concatenate(parts or [np.zeros((1, 3), np.uint8)])
    if not len(joined):
        joined = np.zeros((1, 3), np.uint8)
    joined = joined[:, None, :]
    palette, _ = quantize(joined, allowed)
    pal = palette.astype(np.int32)
    sprite.palette = [tuple(int(c) for c in p) for p in palette] + \
        [(0, 0, 0)] * (256 - len(palette))
    if fmt == "alphatest":
        sprite.palette[255] = key_colour
    for im, rgb in zip(images, stacked, strict=True):
        flat = rgb.reshape(-1, 3).astype(np.int32)
        idx = np.empty(len(flat), np.uint8)
        for start in range(0, len(flat), 4096):
            chunk = flat[start:start + 4096]
            idx[start:start + 4096] = ((chunk[:, None, :] - pal[None]) ** 2).sum(2).argmin(1)
        idx = idx.reshape(rgb.shape[:2])
        if fmt == "alphatest":
            idx[im[..., 3] < 128] = 255
        sprite.frames.append(Frame(im.shape[1], im.shape[0], idx.tobytes()))
    return sprite


__all__ = ["FORMATS", "Frame", "SprError", "Sprite", "TYPES", "from_images", "quantize",
           "read_spr", "write_spr"]
