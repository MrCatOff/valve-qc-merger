"""Exact texel UVs for SMDs that name whole texels.

studiomdl turns an SMD UV into a texel as ``int(u * (width - 1))`` (and
``t`` from ``int(v * (height - 1))``, flipped). A UV that names a texel's
EDGE is a coin toss after the 6 decimals an SMD keeps, and one written on
another scale is off outright:

- ``u = s / width`` — our decompiler before 0.22 (and the CSO hands asset
  made with it): every texel but the first one short, so UV islands slid
  onto the texture's black background (dark seams on the hands);
- ``u = s / (width - 1)`` — other decompilers: about half the vertices a
  texel short.

Both are rewritten to the texel's middle on studiomdl's scale
(:func:`..mdl.decompile._texel_uv`). Only a texture whose every UV is whole
on exactly one of the two scales is touched — an artist's UVs, or ones
already fixed, stay as they are.
"""

from __future__ import annotations

import struct
from pathlib import Path

from valve_qc_merger.mdl.decompile import _texel_uv

_TOLERANCE = 2e-3  # of a texel: the 6 decimals an SMD keeps


def _bmp_sizes(directory: Path) -> dict[str, tuple[int, int]]:
    sizes: dict[str, tuple[int, int]] = {}
    for path in directory.rglob("*"):
        if path.suffix.lower() != ".bmp" or not path.is_file():
            continue
        try:
            with open(path, "rb") as handle:
                head = handle.read(26)
            width, height = struct.unpack("<ii", head[18:26])
        except (OSError, struct.error):
            continue
        sizes.setdefault(path.name.lower(), (abs(width), abs(height)))
    return sizes


MOSTLY = 0.95  # of a texture's UVs whole on one scale: written by a decompiler


def _near(value: float) -> int | None:
    near = round(value)
    return near if abs(value - near) <= _TOLERANCE else None


def texels(uvs: list[tuple[float, float]], width: int, height: int
           ) -> list[tuple[int, int] | None] | None:
    """The texel ``(s, t)`` (t from the top) each of ``uvs`` names, None for
    one that names none (a hand-made part of a decompiled mesh); None in all
    when the texture's UVs are not a decompiler's (see the module)."""
    old = [(_near(u * width), _near((1.0 - v) * height)) for u, v in uvs]
    edge = [(_near(u * (width - 1)), _near(v * (height - 1))) for u, v in uvs]
    whole_old = sum(None not in st for st in old) / max(len(uvs), 1)
    whole_edge = sum(None not in st for st in edge) / max(len(uvs), 1)
    if whole_old >= MOSTLY and whole_edge < 0.5:
        found = [st if None not in st else None for st in old]
    elif whole_edge >= MOSTLY and whole_old < 0.5:
        found = [(st[0], height - 1 - st[1]) if None not in st else None for st in edge]
    else:
        return None  # an artist's UVs, already fixed, or 0 and 1 only: leave them
    if not all(st is None or (0 <= st[0] < width and 0 <= st[1] < height) for st in found):
        return None
    return found  # type: ignore[return-value]


def centred(uvs: list[tuple[float, float]], width: int, height: int
            ) -> list[tuple[float, float]] | None:
    """``uvs`` moved to their texels' middles, or None (nothing to fix)."""
    found = texels(uvs, width, height)
    if found is None:
        return None
    out = []
    for uv, st in zip(uvs, found, strict=True):
        if st is None:
            out.append(uv)
        else:
            mid = _texel_uv(st[0], st[1], width, height)
            out.append((mid.u, mid.v))
    return out


def fix_smd(path: Path, sizes: dict[str, tuple[int, int]]) -> bool:
    """Rewrite ``path``'s whole-texel UVs (see the module); True when it changed."""
    lines = path.read_text(encoding="latin-1").split("\n")
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "triangles")
    except StopIteration:
        return False
    by_material: dict[str, list[int]] = {}  # vertex line indices
    i = start + 1
    while i + 3 < len(lines) and lines[i].strip() != "end":
        by_material.setdefault(lines[i].strip().lower(), []).extend((i + 1, i + 2, i + 3))
        i += 4
    changed = False
    for material, indices in by_material.items():
        size = sizes.get(material)
        rows = [lines[k].split() for k in indices]
        if size is None or any(len(r) < 9 for r in rows):
            continue
        new = centred([(float(r[7]), float(r[8])) for r in rows], *size)
        if new is None:
            continue
        for k, r, (u, v) in zip(indices, rows, new, strict=True):
            r[7], r[8] = f"{u:.6f}", f"{v:.6f}"
            lines[k] = "  " + " ".join(r)  # as the SMD writer lays it out
        changed = True
    if changed:
        path.write_text("\n".join(lines), encoding="latin-1")
    return changed


def fix_directory(directory: Path) -> int:
    """Fix every mesh SMD under ``directory``; returns how many changed."""
    directory = Path(directory)
    sizes = _bmp_sizes(directory)
    if not sizes:
        return 0
    changed = 0
    for path in sorted(directory.rglob("*")):
        if path.suffix.lower() != ".smd" or not path.is_file():
            continue
        if "anims" in {p.lower() for p in path.relative_to(directory).parts[:-1]}:
            continue  # animations hold no triangles
        changed += fix_smd(path, sizes)
    return changed


__all__ = ["centred", "fix_directory", "fix_smd", "texels"]
