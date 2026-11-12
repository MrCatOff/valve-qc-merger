"""A map seen from above: floors in their textures' colours, shaded by
height, with the spawns and marked entities drawn on top.

The world's faces that look up (``nz > 0.3``: floors and slopes; sky,
triggers, clips left out) are filled one convex polygon at a time into a
height buffer (the highest floor wins), each in its texture's average
colour (embedded, or from the map's WADs; grey when neither has it). A
``zmin``/``zmax`` slice keeps only the floors in a height range (one storey
of a building). :class:`Plan` keeps the world <-> pixel mapping.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.geometry import geometry
from valve_qc_merger.mapkit.textures import (
    WadIndex,
    embedded_colour,
    is_special,
    miptex_colour,
)

GREY = (128, 128, 128)
CT_COLOUR = (80, 150, 255)
T_COLOUR = (255, 90, 80)
MARK_COLOUR = (255, 210, 40)
OLD_COLOUR = (150, 150, 150)
GRID = 512.0
FLOOR_NZ = 0.3
MARGIN = 16


@dataclass
class Plan:
    image: np.ndarray  # (H, W, 4) uint8
    scale: float  # pixels per unit
    left: float  # world x at pixel column 0
    top: float  # world y at pixel row 0 (y grows up in the world, down in the image)
    notes: list[str] = field(default_factory=list)
    heights: np.ndarray | None = None  # (H, W) floor height under each pixel, -inf: none

    def floor_at(self, x: float, y: float) -> float | None:
        """The height of the floor drawn at world ``x``, ``y`` (None: none)."""
        if self.heights is None:
            return None
        column, row = self.to_pixel(x, y)
        r, c = int(row), int(column)
        if not (0 <= r < self.heights.shape[0] and 0 <= c < self.heights.shape[1]):
            return None
        value = float(self.heights[r, c])
        return value if np.isfinite(value) else None

    def to_pixel(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.left) * self.scale, (self.top - y) * self.scale

    def to_world(self, column: float, row: float) -> tuple[float, float]:
        return self.left + column / self.scale, self.top - row / self.scale

    def png(self) -> bytes:
        from valve_qc_merger.preview.render import encode_png
        return encode_png(self.image)

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.write_bytes(self.png())
        return path


def texture_colours(bsp: B.Bsp, wads: list[WadIndex]) -> list[tuple[int, int, int]]:
    """The average colour of each texture of the map (grey if unknown)."""
    out = []
    for tex in geometry(bsp).textures:
        colour = embedded_colour(bsp, tex) if tex.embedded else None
        if colour is None and tex.name:
            for wad in wads:
                lump = wad.lump(tex.name)
                if lump is not None:
                    colour = miptex_colour(lump)
                    break
        out.append(colour or GREY)
    return out


def _fill(image: np.ndarray, height: np.ndarray, poly: np.ndarray, plane: tuple[float, ...],
          colour: np.ndarray) -> None:
    """Fill convex polygon ``poly`` (pixel x, y) where its plane's height
    is above what the buffer holds."""
    h, w = height.shape
    x0, y0 = max(int(math.floor(poly[:, 0].min())), 0), max(int(math.floor(poly[:, 1].min())), 0)
    x1 = min(int(math.ceil(poly[:, 0].max())), w - 1)
    y1 = min(int(math.ceil(poly[:, 1].max())), h - 1)
    if x1 < x0 or y1 < y0:
        return
    xs = np.arange(x0, x1 + 1) + 0.5
    ys = np.arange(y0, y1 + 1)[:, None] + 0.5
    inside = np.ones((y1 - y0 + 1, x1 - x0 + 1), bool)
    nxt = np.roll(poly, -1, axis=0)
    area = float(((poly[:, 0] * nxt[:, 1]) - (nxt[:, 0] * poly[:, 1])).sum())
    if abs(area) < 1e-9:
        # a sliver under one pixel: mark the pixel it lies in
        cx, cy = int(poly[:, 0].mean()), int(poly[:, 1].mean())
        if 0 <= cx < w and 0 <= cy < h:
            inside = np.zeros_like(inside)
            inside[min(cy - y0, inside.shape[0] - 1), min(cx - x0, inside.shape[1] - 1)] = True
        else:
            return
    else:
        sign = 1.0 if area > 0 else -1.0
        for (ax, ay), (bx, by) in zip(poly, nxt, strict=True):
            inside &= sign * ((bx - ax) * (ys - ay) - (by - ay) * (xs - ax)) >= -1e-6
    if not inside.any():
        return
    a, b, c = plane  # height = a * px + b * py + c
    z = a * xs + b * ys + c
    region = height[y0:y1 + 1, x0:x1 + 1]
    win = inside & (z > region)
    region[win] = z[win]
    image[y0:y1 + 1, x0:x1 + 1][win] = colour


def _disk(image: np.ndarray, cx: float, cy: float, radius: float,
          colour: tuple[int, int, int]) -> None:
    h, w = image.shape[:2]
    r = max(radius, 2.5)
    x0, x1 = max(int(cx - r - 1), 0), min(int(cx + r + 1), w - 1)
    y0, y1 = max(int(cy - r - 1), 0), min(int(cy + r + 1), h - 1)
    if x1 < x0 or y1 < y0:
        return
    xs = np.arange(x0, x1 + 1) + 0.5
    ys = np.arange(y0, y1 + 1)[:, None] + 0.5
    d = np.hypot(xs - cx, ys - cy)
    region = image[y0:y1 + 1, x0:x1 + 1]
    region[d <= r + 0.8] = (20, 20, 20, 255)
    region[d <= r] = (*colour, 255)


def _line(image: np.ndarray, a: tuple[float, float], b: tuple[float, float],
          colour: tuple[int, int, int], width: float = 1.0) -> None:
    h, w = image.shape[:2]
    steps = int(max(abs(b[0] - a[0]), abs(b[1] - a[1]), 1) * 2)
    t = np.linspace(0.0, 1.0, steps + 1)
    xs = a[0] + (b[0] - a[0]) * t
    ys = a[1] + (b[1] - a[1]) * t
    for dx in np.arange(-width / 2, width / 2 + 0.01, 0.5) if width > 1 else (0.0,):
        cols = np.clip((xs + dx).astype(int), 0, w - 1)
        for dy in (np.arange(-width / 2, width / 2 + 0.01, 0.5) if width > 1 else (0.0,)):
            rows = np.clip((ys + dy).astype(int), 0, h - 1)
            image[rows, cols] = (*colour, 255)


def _cross(image: np.ndarray, cx: float, cy: float, size: float) -> None:
    s = max(size, 4.0)
    _line(image, (cx - s, cy - s), (cx + s, cy + s), MARK_COLOUR, 2.0)
    _line(image, (cx - s, cy + s), (cx + s, cy - s), MARK_COLOUR, 2.0)


def render_plan(bsp: B.Bsp, *, size: int = 1024, wads: list[WadIndex] | None = None,
                entities: list[dict[str, str]] | None = None, spawns: bool = True,
                marks: list[int] | None = None, zmin: float | None = None,
                zmax: float | None = None, grid: bool = True,
                grey_spawns_before: int | None = None) -> Plan:
    """The plan of ``bsp`` with its longest side ``size`` pixels (see the
    module). ``entities``: the entity list to draw spawns and ``marks``
    (entity indices) from — default the map's own. ``grey_spawns_before``:
    spawns of entities before this index are drawn grey (the map's own,
    the new ones in colour)."""
    from valve_qc_merger.server.entities import parse

    geo = geometry(bsp)
    entities = entities if entities is not None else parse(bsp.entity_text)
    colours = np.array(texture_colours(bsp, wads or []), np.float64).reshape(-1, 3)
    faces = geo.model_faces(0)
    normals = geo.face_normals()[faces]
    tex = geo.face_texture()[faces]
    names = [t.name for t in geo.textures]
    keep = normals[:, 2] > FLOOR_NZ
    keep &= np.array([not (0 <= t < len(names) and is_special(names[t])) for t in tex], bool)
    faces, normals, tex = faces[keep], normals[keep], tex[keep]
    polys = [geo.face_points(int(f)).astype(np.float64) for f in faces]
    centre_z = np.array([p[:, 2].mean() for p in polys]) if polys else np.zeros(0)
    if zmin is not None or zmax is not None:
        inside = np.ones(len(polys), bool)
        if zmin is not None:
            inside &= centre_z >= zmin
        if zmax is not None:
            inside &= centre_z <= zmax
        faces, normals, tex = faces[inside], normals[inside], tex[inside]
        polys = [p for p, k in zip(polys, inside, strict=True) if k]
        centre_z = centre_z[inside]
    if polys:
        allpts = np.concatenate(polys)
        lo, hi = allpts.min(axis=0), allpts.max(axis=0)
    else:
        lo, hi = geo.bounds(0)
    span = max(hi[0] - lo[0], hi[1] - lo[1], 1.0)
    scale = (size - 2 * MARGIN) / span
    width = int(math.ceil((hi[0] - lo[0]) * scale)) + 2 * MARGIN
    height = int(math.ceil((hi[1] - lo[1]) * scale)) + 2 * MARGIN
    left, top = lo[0] - MARGIN / scale, hi[1] + MARGIN / scale
    image = np.zeros((height, width, 4), np.uint8)
    image[..., :3] = (24, 26, 30)
    image[..., 3] = 255
    zbuf = np.full((height, width), -np.inf)
    zlo, zhi = (float(lo[2]), float(hi[2])) if polys else (0.0, 1.0)
    zspan = max(zhi - zlo, 1.0)
    planes = geo.planes
    for poly, face, normal, t in zip(polys, faces, normals, tex, strict=True):
        px = (poly[:, 0] - left) * scale
        py = (top - poly[:, 1]) * scale
        nx, ny, nz = normal
        f = geo.faces[face]
        dist = float(planes["dist"][f["plane"]]) * (-1.0 if f["side"] else 1.0)
        # world z = (dist - nx x - ny y) / nz, x = left + px / s, y = top - py / s
        a = -nx / (nz * scale)
        b = ny / (nz * scale)
        c = (dist - nx * left - ny * top) / nz
        base = colours[t] if 0 <= t < len(colours) else np.array(GREY, np.float64)
        light = (0.5 + 0.5 * (poly[:, 2].mean() - zlo) / zspan) * (0.75 + 0.25 * nz)
        rgb = np.clip(base * light, 0, 255).astype(np.uint8)
        _fill(image, zbuf, np.stack([px, py], axis=1), (a, b, c),
              np.array([*rgb, 255], np.uint8))
    _level(image, np.isfinite(zbuf))
    plan = Plan(image, scale, left, top, heights=zbuf)
    if grid:
        _grid(plan)
    if spawns:
        for number, entity in enumerate(entities):
            colour = {"info_player_start": CT_COLOUR,
                      "info_player_deathmatch": T_COLOUR}.get(entity.get("classname", ""))
            if colour is not None and grey_spawns_before is not None \
                    and number < grey_spawns_before:
                colour = OLD_COLOUR
            origin = _xyz(entity.get("origin", ""))
            if colour is None or origin is None:
                continue
            cx, cy = plan.to_pixel(origin[0], origin[1])
            yaw = math.radians(_yaw(entity.get("angles", "")))
            tip = plan.to_pixel(origin[0] + 40 * math.cos(yaw), origin[1] + 40 * math.sin(yaw))
            _line(image, (cx, cy), tip, colour, 1.5)
            _disk(image, cx, cy, 16 * scale, colour)
    for index in marks or []:
        if 0 <= index < len(entities):
            origin = _xyz(entities[index].get("origin", ""))
            if origin is None:
                continue
            cx, cy = plan.to_pixel(origin[0], origin[1])
            _cross(image, cx, cy, 12 * scale)
    if not polys:
        plan.notes.append("no floors to draw")
    return plan


def _level(image: np.ndarray, filled: np.ndarray) -> None:
    """Brighten the floors so the brightest few percent reach near white
    (CSO textures are dark; the plan should read at a glance)."""
    if not filled.any():
        return
    rgb = image[..., :3][filled].astype(np.float64)
    bright = float(np.percentile(rgb.max(axis=1), 97))
    if bright < 1:
        return
    gain = min(235.0 / bright, 4.0)
    image[..., :3][filled] = np.clip(rgb * gain, 0, 255).astype(np.uint8)


def _grid(plan: Plan) -> None:
    h, w = plan.image.shape[:2]
    x_world, _ = plan.to_world(0, 0)
    x_end, _ = plan.to_world(w, 0)
    _, y_top = plan.to_world(0, 0)
    _, y_end = plan.to_world(0, h)
    shade = np.array([255, 255, 255], np.float64)
    for x in np.arange(math.ceil(x_world / GRID) * GRID, x_end, GRID):
        col = int(plan.to_pixel(x, 0)[0])
        if 0 <= col < w:
            px = plan.image[:, col, :3].astype(np.float64)
            plan.image[:, col, :3] = (px * 0.82 + shade * 0.18).astype(np.uint8)
    for y in np.arange(math.ceil(y_end / GRID) * GRID, y_top, GRID):
        row = int(plan.to_pixel(0, y)[1])
        if 0 <= row < h:
            px = plan.image[row, :, :3].astype(np.float64)
            plan.image[row, :, :3] = (px * 0.82 + shade * 0.18).astype(np.uint8)


def _xyz(text: str) -> tuple[float, float, float] | None:
    try:
        x, y, z = (float(v) for v in text.split()[:3])
    except ValueError:
        return None
    return x, y, z


def _yaw(text: str) -> float:
    parts = text.split()
    try:
        return float(parts[1]) if len(parts) >= 2 else 0.0
    except ValueError:
        return 0.0


__all__ = ["Plan", "render_plan", "texture_colours"]
