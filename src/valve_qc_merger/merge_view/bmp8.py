"""8-bit palettised BMP operations for merge-v textures, pure Python.

GoldSource textures are 8-bit indexed BMPs; masked textures keep their
transparent colour at palette index 255. Everything here works on small
(<=512x512) images, so plain Python loops are fast enough.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

MASK_INDEX = 255


class BmpError(ValueError):
    """Not an 8-bit uncompressed BMP we can process."""


@dataclass
class Bmp8:
    """Top-down, row-major 8-bit image with a 256-entry RGB palette."""

    width: int
    height: int
    palette: list[tuple[int, int, int]]  # exactly 256 entries
    pixels: bytearray  # width * height palette indices, top-down


def read_bmp8(data: bytes) -> Bmp8:
    if len(data) < 54 or data[:2] != b"BM":
        raise BmpError("not a BMP file")
    pixel_offset = struct.unpack_from("<I", data, 10)[0]
    header_size = struct.unpack_from("<I", data, 14)[0]
    width, height = struct.unpack_from("<ii", data, 18)
    planes, bits = struct.unpack_from("<HH", data, 26)
    compression = struct.unpack_from("<I", data, 30)[0]
    clr_used = struct.unpack_from("<I", data, 46)[0]
    if bits != 8 or compression != 0:
        raise BmpError(f"need 8-bit uncompressed BMP (got {bits}-bit, "
                       f"compression {compression})")
    top_down = height < 0
    height = abs(height)
    colours = clr_used or 256
    palette_offset = 14 + header_size
    palette: list[tuple[int, int, int]] = []
    for i in range(256):
        if i < colours:
            b, g, r, _ = data[palette_offset + i * 4: palette_offset + i * 4 + 4]
            palette.append((r, g, b))
        else:
            palette.append((0, 0, 0))
    stride = (width + 3) & ~3
    pixels = bytearray(width * height)
    for row in range(height):
        src_row = row if top_down else height - 1 - row
        start = pixel_offset + src_row * stride
        pixels[row * width:(row + 1) * width] = data[start:start + width]
    return Bmp8(width, height, palette, pixels)


def write_bmp8(image: Bmp8) -> bytes:
    stride = (image.width + 3) & ~3
    pixel_bytes = stride * image.height
    header = struct.pack(
        "<2sIHHI", b"BM", 14 + 40 + 1024 + pixel_bytes, 0, 0, 14 + 40 + 1024,
    )
    info = struct.pack(
        "<IiiHHIIiiII", 40, image.width, image.height, 1, 8, 0,
        pixel_bytes, 2835, 2835, 256, 256,
    )
    palette = bytearray()
    for r, g, b in image.palette:
        palette += bytes((b, g, r, 0))
    rows = bytearray()
    pad = bytes(stride - image.width)
    for row in range(image.height - 1, -1, -1):  # bottom-up
        rows += image.pixels[row * image.width:(row + 1) * image.width]
        rows += pad
    return bytes(header) + info + bytes(palette) + bytes(rows)


def to_rgb(image: Bmp8) -> list[tuple[int, int, int]]:
    palette = image.palette
    return [palette[i] for i in image.pixels]


def resample_rgb(
    rgb: list[tuple[int, int, int]], width: int, height: int,
    new_width: int, new_height: int,
    mask: list[bool] | None = None,
) -> tuple[list[tuple[int, int, int]], list[bool]]:
    """Box-filter resample; a target pixel is masked when most sources are."""
    out: list[tuple[int, int, int]] = []
    out_mask: list[bool] = []
    for y in range(new_height):
        y0 = y * height // new_height
        y1 = max(y0 + 1, (y + 1) * height // new_height)
        for x in range(new_width):
            x0 = x * width // new_width
            x1 = max(x0 + 1, (x + 1) * width // new_width)
            r = g = b = opaque = masked = 0
            for sy in range(y0, y1):
                base = sy * width
                for sx in range(x0, x1):
                    if mask is not None and mask[base + sx]:
                        masked += 1
                        continue
                    pr, pg, pb = rgb[base + sx]
                    r += pr
                    g += pg
                    b += pb
                    opaque += 1
            if opaque and masked <= opaque:
                out.append((r // opaque, g // opaque, b // opaque))
                out_mask.append(False)
            else:
                out.append((0, 0, 0))
                out_mask.append(True)
    return out, out_mask


def median_cut(
    rgb: list[tuple[int, int, int]], colours: int,
) -> list[tuple[int, int, int]]:
    """Median-cut palette over the given pixels (deterministic)."""
    unique = sorted(set(rgb))
    if len(unique) <= colours:
        return unique + [(0, 0, 0)] * (colours - len(unique))
    boxes = [unique]
    while len(boxes) < colours:
        # Split the box with the largest channel spread.
        best_index, best_spread, best_channel = -1, -1, 0
        for index, box in enumerate(boxes):
            if len(box) < 2:
                continue
            for channel in range(3):
                values = [p[channel] for p in box]
                spread = max(values) - min(values)
                if spread > best_spread:
                    best_index, best_spread, best_channel = index, spread, channel
        if best_index < 0:
            break
        box = sorted(boxes.pop(best_index), key=lambda p: p[best_channel])
        half = len(box) // 2
        boxes.append(box[:half])
        boxes.append(box[half:])
    palette = []
    for box in boxes:
        n = len(box)
        palette.append((sum(p[0] for p in box) // n,
                        sum(p[1] for p in box) // n,
                        sum(p[2] for p in box) // n))
    palette.sort()
    return palette + [(0, 0, 0)] * (colours - len(palette))


def quantise(
    rgb: list[tuple[int, int, int]],
    palette: list[tuple[int, int, int]],
    colours: int,
) -> bytearray:
    """Map pixels to nearest palette entries (first ``colours`` slots)."""
    cache: dict[tuple[int, int, int], int] = {}
    out = bytearray(len(rgb))
    candidates = palette[:colours]
    for i, pixel in enumerate(rgb):
        index = cache.get(pixel)
        if index is None:
            r, g, b = pixel
            index = min(
                range(len(candidates)),
                key=lambda j: ((candidates[j][0] - r) ** 2
                               + (candidates[j][1] - g) ** 2
                               + (candidates[j][2] - b) ** 2),
            )
            cache[pixel] = index
        out[i] = index
    return out


__all__ = ["Bmp8", "BmpError", "MASK_INDEX", "median_cut", "quantise",
           "read_bmp8", "resample_rgb", "to_rgb", "write_bmp8"]
