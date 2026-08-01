"""Texture downscale and 2x2 atlas packing for merge-view (spec 3.11-3.12).

GoldSource caps textures per model (studiomdl degrades far earlier), so four
256x256 textures can share one 512x512 BMP: each participating texture is
resampled to 256x256, composited into a tile, and every referencing vertex UV
is remapped into that tile with a half-texel inset so bilinear filtering
cannot bleed a neighbour tile across the seam.

Grouping rules (never mixed in one atlas):
- ``masked`` textures pack only with masked ones - transparency lives at
  palette index 255, which stays reserved in the shared palette;
- ``chrome``/``additive``/``fullbright`` textures never pack (chrome UVs are
  engine-generated; additive glow layers keep their own palettes);
- textures referenced with tiling UVs (outside [0,1]) never pack;
- ``--no-pack-texture`` globs keep a texture standalone.

A group of 2-3 leftovers still packs (remaining tiles stay black); a single
leftover stays standalone.
"""

from __future__ import annotations

import dataclasses
import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.merge_view.bmp8 import (
    MASK_INDEX,
    Bmp8,
    BmpError,
    median_cut,
    quantise,
    read_bmp8,
    resample_rgb,
    to_rgb,
    write_bmp8,
)
from valve_qc_merger.models.geometry import Vector2
from valve_qc_merger.models.smd import Smd

TILE = 256
ATLAS = 512
_UV_EPSILON = 1e-3


@dataclass
class TextureOptions:
    """CLI surface for the texture stage."""

    max_size: int | None = None
    pack: bool = False
    no_pack: list[str] = field(default_factory=list)


def downscale_textures(
    out_dir: Path, staged: list[str], max_size: int, warnings: list[str],
) -> int:
    """Resample every staged BMP larger than ``max_size`` on either axis."""
    changed = 0
    for name in staged:
        path = out_dir / name
        try:
            image = read_bmp8(path.read_bytes())
        except (OSError, BmpError) as exc:
            warnings.append(f"downscale skipped {name}: {exc}")
            continue
        if image.width <= max_size and image.height <= max_size:
            continue
        scale = max_size / max(image.width, image.height)
        new_w = max(1, round(image.width * scale))
        new_h = max(1, round(image.height * scale))
        masked = [i == MASK_INDEX for i in image.pixels]
        has_mask = any(masked)
        rgb, mask = resample_rgb(
            to_rgb(image), image.width, image.height, new_w, new_h,
            masked if has_mask else None,
        )
        colours = 255 if has_mask else 256
        palette = median_cut([p for p, m in zip(rgb, mask, strict=True) if not m] or [(0, 0, 0)],
                             colours)
        pixels = quantise(rgb, palette, colours)
        if has_mask:
            palette = palette[:255] + [image.palette[MASK_INDEX]]
            for i, m in enumerate(mask):
                if m:
                    pixels[i] = MASK_INDEX
        palette += [(0, 0, 0)] * (256 - len(palette))
        path.write_bytes(write_bmp8(Bmp8(new_w, new_h, palette[:256], pixels)))
        changed += 1
    return changed


def _tiling_materials(meshes: list[Smd]) -> set[str]:
    """Materials referenced with UVs outside [0,1] cannot move into a tile."""
    tiling: set[str] = set()
    for smd in meshes:
        for triangle in smd.triangles:
            material = triangle.material.lower()
            if material in tiling:
                continue
            for vertex in triangle.vertices:
                if not (-_UV_EPSILON <= vertex.uv.u <= 1 + _UV_EPSILON
                        and -_UV_EPSILON <= vertex.uv.v <= 1 + _UV_EPSILON):
                    tiling.add(material)
                    break
    return tiling


def _load_tile(path: Path, masked: bool) -> tuple[list[tuple[int, int, int]], list[bool]]:
    image = read_bmp8(path.read_bytes())
    mask = [i == MASK_INDEX for i in image.pixels] if masked else None
    return resample_rgb(to_rgb(image), image.width, image.height, TILE, TILE, mask)


def pack_textures(
    out_dir: Path,
    meshes: list[Smd],
    staged: list[str],
    render_modes: dict[str, str],
    options: TextureOptions,
    warnings: list[str],
) -> dict[str, str]:
    """Pack eligible staged textures four-to-a-file; rewrite mesh UVs.

    Returns original staged name -> ``atlas.bmp:tile`` (tile 0..3, row-major).
    Mutates the mesh SMDs (materials + UVs), deletes packed files and writes
    the atlas BMPs; the caller refreshes ``$texrendermode`` bookkeeping from
    ``render_modes`` (packed entries are removed, masked atlases added).
    """
    tiling = _tiling_materials(meshes)
    eligible_plain: list[str] = []
    eligible_masked: list[str] = []
    for name in sorted(staged):
        mode = render_modes.get(name)
        if mode in ("additive", "chrome", "fullbright", "flatshade"):
            continue
        if name.lower() in tiling:
            warnings.append(f"not packed (tiling UVs): {name}")
            continue
        if any(fnmatch.fnmatch(name, glob) for glob in options.no_pack):
            continue
        try:
            read_bmp8((out_dir / name).read_bytes())
        except (OSError, BmpError) as exc:
            warnings.append(f"not packed ({exc}): {name}")
            continue
        (eligible_masked if mode == "masked" else eligible_plain).append(name)

    mapping: dict[str, str] = {}
    atlas_number = 0
    for group_names, masked in ((eligible_plain, False), (eligible_masked, True)):
        for start in range(0, len(group_names), 4):
            members = group_names[start:start + 4]
            if len(members) < 2:
                continue  # a lone leftover stays standalone
            atlas_number += 1
            atlas_name = f"atlas{'m' if masked else ''}{atlas_number:02d}.bmp"
            rgb = [(0, 0, 0)] * (ATLAS * ATLAS)
            mask = [masked] * (ATLAS * ATLAS)
            for tile_index, member in enumerate(members):
                tile_rgb, tile_mask = _load_tile(out_dir / member, masked)
                row, col = divmod(tile_index, 2)
                for y in range(TILE):
                    dst = (row * TILE + y) * ATLAS + col * TILE
                    src = y * TILE
                    rgb[dst:dst + TILE] = tile_rgb[src:src + TILE]
                    mask[dst:dst + TILE] = tile_mask[src:src + TILE]
                mapping[member] = f"{atlas_name}:{tile_index}"
            colours = 255 if masked else 256
            opaque = [p for p, m in zip(rgb, mask, strict=True) if not m]
            palette = median_cut(opaque or [(0, 0, 0)], colours)
            pixels = quantise(rgb, palette, colours)
            if masked:
                palette = palette[:255] + [(0, 0, 255)]
                for i, m in enumerate(mask):
                    if m:
                        pixels[i] = MASK_INDEX
            palette += [(0, 0, 0)] * (256 - len(palette))
            (out_dir / atlas_name).write_bytes(
                write_bmp8(Bmp8(ATLAS, ATLAS, palette[:256], pixels))
            )
            for member in members:
                (out_dir / member).unlink(missing_ok=True)

    if mapping:
        _rewrite_uvs(meshes, mapping)
    return mapping


def _rewrite_uvs(meshes: list[Smd], mapping: dict[str, str]) -> None:
    """Move every referencing vertex UV into its atlas tile (half-texel inset).

    SMD v runs bottom-up while BMP rows run top-down, so the tile ROW is
    mirrored for the v axis.
    """
    inset = 0.5 / TILE
    span = 1.0 - 2.0 * inset
    by_material = {name.lower(): target for name, target in mapping.items()}
    for smd in meshes:
        new_triangles = []
        for triangle in smd.triangles:
            target = by_material.get(triangle.material.lower())
            if target is None:
                new_triangles.append(triangle)
                continue
            atlas_name, tile = target.split(":")
            row, col = divmod(int(tile), 2)
            vrow = 1 - row  # v axis is bottom-up
            a, b, c = (
                dataclasses.replace(v, uv=Vector2(
                    (col + inset + min(max(v.uv.u, 0.0), 1.0) * span) / 2.0,
                    (vrow + inset + min(max(v.uv.v, 0.0), 1.0) * span) / 2.0,
                ))
                for v in triangle.vertices
            )
            vertices = (a, b, c)
            new_triangles.append(dataclasses.replace(
                triangle, material=atlas_name, vertices=vertices,
            ))
        smd.triangles = new_triangles


__all__ = ["ATLAS", "TILE", "TextureOptions", "downscale_textures",
           "pack_textures"]
