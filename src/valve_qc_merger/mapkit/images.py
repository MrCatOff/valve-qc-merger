"""Read a picture into an (h, w, 4) uint8 RGBA array — PNG, TGA or BMP, in
pure Python (no Pillow, no Qt), for texture replacement on the command
line and in tests.

PNG: 8-bit greyscale, RGB, palette (with ``tRNS``), grey + alpha and RGBA,
every row filter, not interlaced. TGA: uncompressed and RLE, 8-bit grey,
24 and 32 bit, either origin. BMP: uncompressed 8, 24 and 32 bit.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np


class ImageError(ValueError):
    """Not a picture this module reads."""


def read_image(source: Path | str | bytes) -> np.ndarray:
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _png(data)
    if data[:2] == b"BM":
        return _bmp(data)
    if isinstance(source, (str, Path)) and str(source).lower().endswith(".tga"):
        return _tga(data)
    try:
        return _tga(data)
    except (ImageError, struct.error, ValueError) as exc:
        raise ImageError("not a PNG, TGA or BMP picture") from exc


# -- PNG ----------------------------------------------------------------------

def _png(data: bytes) -> np.ndarray:
    pos, idat = 8, bytearray()
    width = height = depth = colour = interlace = 0
    palette = np.zeros((256, 4), np.uint8)
    palette[:, 3] = 255
    trns: bytes = b""
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, colour, _c, _f, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            entries = np.frombuffer(body, np.uint8).reshape(-1, 3)
            palette[:len(entries), :3] = entries
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
        pos += 12 + length
    if depth != 8 or interlace:
        raise ImageError(f"PNG with {depth}-bit samples{' interlaced' if interlace else ''}: "
                         "save it as 8-bit, not interlaced")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour)
    if channels is None:
        raise ImageError(f"PNG colour type {colour}")
    stride = width * channels
    raw = np.frombuffer(zlib.decompress(bytes(idat)), np.uint8)
    rows = raw[:height * (stride + 1)].reshape(height, stride + 1)
    out = np.zeros((height, stride), np.uint8)
    previous = np.zeros(stride, np.int32)
    for y in range(height):
        kind, line = rows[y, 0], rows[y, 1:].astype(np.int32)
        if kind == 0:
            current = line
        elif kind == 1:  # sub: a running sum per channel
            current = np.cumsum(line.reshape(width, channels), axis=0).ravel() & 0xFF
        elif kind == 2:
            current = (line + previous) & 0xFF
        elif kind in (3, 4):
            cur = np.zeros((width, channels), np.int32)
            px = line.reshape(width, channels)
            up_row = previous.reshape(width, channels)
            zero = np.zeros(channels, np.int32)
            for x in range(width):
                left = cur[x - 1] if x else zero
                up = up_row[x]
                if kind == 3:
                    cur[x] = (px[x] + ((left + up) >> 1)) & 0xFF
                else:
                    corner = up_row[x - 1] if x else zero
                    p = left + up - corner
                    pa, pb, pc = np.abs(p - left), np.abs(p - up), np.abs(p - corner)
                    pred = np.where((pa <= pb) & (pa <= pc), left, np.where(pb <= pc, up, corner))
                    cur[x] = (px[x] + pred) & 0xFF
            current = cur.ravel()
        else:
            raise ImageError(f"PNG row filter {kind}")
        out[y] = current
        previous = current
    pixels = out.reshape(height, width, channels)
    if colour == 6:
        return pixels.copy()
    rgba = np.empty((height, width, 4), np.uint8)
    if colour == 2:
        rgba[..., :3] = pixels
        rgba[..., 3] = 255
        if len(trns) >= 6:
            key = struct.unpack(">3H", trns[:6])
            rgba[..., 3] = np.where((pixels == np.array(key, np.uint8)).all(axis=2), 0, 255)
    elif colour == 0:
        rgba[..., :3] = pixels
        rgba[..., 3] = 255
    elif colour == 4:
        rgba[..., :3] = pixels[..., :1]
        rgba[..., 3] = pixels[..., 1]
    else:  # palette
        if trns:
            palette[:len(trns), 3] = np.frombuffer(trns, np.uint8)
        rgba = palette[pixels[..., 0]]
    return rgba


# -- TGA ----------------------------------------------------------------------

def _tga(data: bytes) -> np.ndarray:
    if len(data) < 18:
        raise ImageError("too short for a TGA")
    id_len, cmap_type, kind = data[0], data[1], data[2]
    cmap_len, cmap_bits = struct.unpack_from("<H", data, 5)[0], data[7]
    width, height, bits, desc = struct.unpack_from("<HHBB", data, 12)
    if kind not in (2, 3, 10, 11) or bits not in (8, 24, 32) or not width or not height:
        raise ImageError(f"TGA type {kind}, {bits} bit")
    pos = 18 + id_len + (cmap_len * ((cmap_bits + 7) // 8) if cmap_type else 0)
    size = bits // 8
    count = width * height
    if kind in (2, 3):
        pixels = np.frombuffer(data, np.uint8, count * size, pos).reshape(count, size)
    else:
        out = bytearray()
        while len(out) < count * size:
            head = data[pos]
            pos += 1
            run = (head & 0x7F) + 1
            if head & 0x80:
                out += data[pos:pos + size] * run
                pos += size
            else:
                out += data[pos:pos + run * size]
                pos += run * size
        pixels = np.frombuffer(bytes(out[:count * size]), np.uint8).reshape(count, size)
    rgba = np.empty((count, 4), np.uint8)
    if size == 1:
        rgba[:, :3] = pixels
        rgba[:, 3] = 255
    else:
        rgba[:, 0], rgba[:, 1], rgba[:, 2] = pixels[:, 2], pixels[:, 1], pixels[:, 0]
        rgba[:, 3] = pixels[:, 3] if size == 4 else 255
    image = rgba.reshape(height, width, 4)
    if not desc & 0x20:  # origin bottom-left
        image = image[::-1]
    return np.ascontiguousarray(image)


# -- BMP ----------------------------------------------------------------------

def _bmp(data: bytes) -> np.ndarray:
    offset = struct.unpack_from("<I", data, 10)[0]
    header = struct.unpack_from("<I", data, 14)[0]
    width, height = struct.unpack_from("<ii", data, 18)
    bits, compression = struct.unpack_from("<H", data, 28)[0], struct.unpack_from("<I", data, 30)[0]
    if compression not in (0, 3) or bits not in (8, 24, 32):
        raise ImageError(f"BMP {bits} bit, compression {compression}")
    flip = height > 0
    height = abs(height)
    stride = (width * bits // 8 + 3) & ~3
    rows = np.frombuffer(data, np.uint8, stride * height, offset).reshape(height, stride)
    if bits == 8:
        colours = struct.unpack_from("<I", data, 46)[0] or 256
        table = np.frombuffer(data, np.uint8, colours * 4, 14 + header).reshape(-1, 4)
        idx = rows[:, :width]
        rgba = np.empty((height, width, 4), np.uint8)
        rgba[..., 0], rgba[..., 1], rgba[..., 2] = (table[idx, 2], table[idx, 1],
                                                    table[idx, 0])
        rgba[..., 3] = 255
    else:
        size = bits // 8
        px = rows[:, :width * size].reshape(height, width, size)
        rgba = np.empty((height, width, 4), np.uint8)
        rgba[..., 0], rgba[..., 1], rgba[..., 2] = px[..., 2], px[..., 1], px[..., 0]
        rgba[..., 3] = px[..., 3] if size == 4 and px[..., 3].any() else 255
    return np.ascontiguousarray(rgba[::-1] if flip else rgba)


__all__ = ["ImageError", "read_image"]
