"""One skin count for a weapon's v_, p_ and w_ model.

A weapon's extra skins are named the same in every manifest —
``<model>_skin<k>``: merge-v makes skin row k a weapon entry of its own,
merge-p / merge-w a ``pev_skin`` k — so a plugin switches all three models
to variant k by one name. CSO ships some weapons with more skins in one
model than in the others (v_jetgun's blue and yellow, w_jetgun blue only;
v_buffng7's 9 ammo screens, its p_ and w_ one). A staged p_ / w_ model gets
as many skin rows as its v_ (and its p_ / w_ sibling):

- rows its sibling has over the same textures are copied with their
  textures (w_jetgun gets p_jetgun's yellow);
- the rest repeat its first row (the v_ model's screens and states have no
  counterpart in a held or dropped weapon: it simply looks as it does).

Only the staged copies change; the assets stay as imported.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from valve_qc_merger.merge_player.merger import parse_texturegroups

if TYPE_CHECKING:
    from valve_qc_merger.project.model import Asset, Project

_BLOCK = re.compile(r"^\s*\$texturegroup[^{]*\{.*?\n\s*\}[ \t]*\n?", re.DOTALL | re.MULTILINE)
_MESH_TEXTURE = re.compile(r"^\s*triangles\s*\n\s*(\S[^\n]*)", re.MULTILINE)
_SEQUENCE = re.compile(r"^\s*\$sequence\b", re.MULTILINE | re.IGNORECASE)


def _qc(directory: Path) -> Path | None:
    return next(iter(sorted(directory.glob("*.qc"))), None)


def _rows(directory: Path) -> list[list[str]]:
    qc = _qc(directory)
    return parse_texturegroups(qc.read_text(encoding="latin-1")) if qc else []


def _textures(directory: Path) -> dict[str, Path]:
    return {p.name.lower(): p for p in directory.rglob("*")
            if p.is_file() and p.suffix.lower() == ".bmp"}


def _mesh_texture(directory: Path) -> str | None:
    """A texture the model's meshes use (the column a padding row names)."""
    for smd in sorted(directory.glob("*.smd")):
        match = _MESH_TEXTURE.search(smd.read_text(encoding="latin-1"))
        if match:
            return match.group(1).strip()
    return None


def _sibling(project: Project, prefix: str, base: str) -> Asset | None:
    wanted = f"{prefix}_{base}".lower()
    return next((a for a in project.assets.values()
                 if a.name.lower() == wanted and not a.derived), None)


def aligned_rows(own: list[list[str]], own_textures: set[str], sibling: list[list[str]],
                 count: int, fallback: str | None) -> tuple[list[list[str]], int]:
    """``own``'s skin rows grown to ``count``: the sibling's rows when they
    are over textures this model uses, else its first row again. Returns
    (rows, how many came from the sibling)."""
    rows = [list(r) for r in own] or ([[fallback]] if fallback else [])
    if not rows:
        return rows, 0
    taken = 0
    base = {t.lower() for t in rows[0]}
    sib_base = {t.lower() for t in sibling[0]} if sibling else set()
    shares = bool(sibling) and (sib_base == base or (len(own) <= 1 and sib_base <= own_textures))
    if shares and len(sibling) > len(rows):
        if len(own) <= 1:
            rows = [list(r) for r in sibling]  # their columns: ours has no texturegroup
        else:
            rows += [list(r) for r in sibling[len(rows):]]
        taken = len(sibling) - max(len(own), 1)
    while len(rows) < count:
        rows.append(list(rows[0]))
    return rows, taken


def _write_rows(qc: Path, rows: list[list[str]]) -> None:
    text = _BLOCK.sub("", qc.read_text(encoding="latin-1"))
    block = ("$texturegroup skinfamilies\n{\n"
             + "".join("    { " + " ".join(f'"{t}"' for t in row) + " }\n" for row in rows)
             + "}\n\n")
    match = _SEQUENCE.search(text)
    at = match.start() if match else len(text)
    qc.write_text(text[:at] + block + text[at:], encoding="latin-1")


def align_staged_skins(project: Project, staged: Path, assets: list[Asset]) -> list[str]:
    """Give every staged p_ / w_ model of ``assets`` as many skin rows as
    its weapon's other models (see the module). Returns what changed."""
    notes: list[str] = []
    for asset in assets:
        kind = asset.name[:2].lower()
        if kind not in ("p_", "w_") or not (staged / asset.name).is_dir():
            continue
        base = asset.name[2:]
        here = staged / asset.name
        own = _rows(here)
        view = _sibling(project, "v", base)
        other = _sibling(project, "w" if kind == "p_" else "p", base)
        view_rows = _rows(project.asset_dir(view.name)) if view else []
        other_dir = project.asset_dir(other.name) if other else None
        other_rows = _rows(other_dir) if other_dir else []
        count = max(len(view_rows), len(other_rows), len(own), 1)
        if count <= max(len(own), 1):
            continue
        textures = _textures(here)
        used = set(textures) | {_mesh_texture(here) or ""}
        rows, taken = aligned_rows(own, {t for t in used if t}, other_rows, count,
                                   _mesh_texture(here))
        if not rows:
            continue
        if taken and other_dir is not None:
            theirs = _textures(other_dir)
            folder = next(iter(textures.values())).parent if textures else here
            for name in {t.lower() for row in rows for t in row} - set(textures):
                if name in theirs:
                    shutil.copy2(theirs[name], folder / theirs[name].name)
        qc = _qc(here)
        if qc is None:
            continue
        _write_rows(qc, rows)
        padded = count - max(len(own), 1) - taken
        notes.append(f"{asset.name}: {max(len(own), 1)} -> {count} skins"
                     + (f" ({taken} from {other.name})" if taken and other else "")
                     + (f" ({padded} as its first)" if padded else ""))
    return notes


__all__ = ["align_staged_skins", "aligned_rows"]
