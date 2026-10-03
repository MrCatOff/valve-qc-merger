"""merge-v input guards: skin families and header commands.

**Skins.** A view model's ``$texturegroup`` (CSO upgrade skins: ``Luger_v_6``,
``Luger_v_8`` …) cannot survive a merge as skins — the server can set a view
model's ``body`` (with the weapon animation) but not its ``skin``. Each extra
skin row therefore becomes its own weapon entry, ``<model>_skin<k>``: the
weapon meshes with that row's textures, the same bones and animations (the
merge dedupes identical sequences, so a variant adds a submodel and textures,
not animation data).

**Header.** The merged QC is written with ``$scale 1.0``, no ``$origin`` and
``$flags 0``. An input that relies on anything else would silently change
when merged, so it is rejected (scale, origin) or warned about (flags).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from valve_qc_merger.merge_player.merger import parse_texturegroups
from valve_qc_merger.merge_view.bodygroups import ModelParts
from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.models.smd import Smd, Triangle

SKIN_SUFFIX = "_skin"

_SCALE_RE = re.compile(r"^\s*\$scale\s+(\S+)", re.IGNORECASE | re.MULTILINE)
_ORIGIN_RE = re.compile(r"^\s*\$origin\s+(\S+)\s+(\S+)\s+(\S+)", re.IGNORECASE | re.MULTILINE)
_FLAGS_RE = re.compile(r"^\s*\$flags\s+(\S+)", re.IGNORECASE | re.MULTILINE)


@dataclass
class HeaderCheck:
    rejects: list[str]
    warnings: list[str]


def check_header(qc_text: str) -> HeaderCheck:
    """What in a model's QC header the merged QC cannot reproduce."""
    rejects: list[str] = []
    warnings: list[str] = []
    for match in _SCALE_RE.finditer(qc_text):
        try:
            scale = float(match.group(1))
        except ValueError:
            rejects.append(f"unreadable $scale {match.group(1)!r}")
            continue
        if abs(scale - 1.0) > 1e-6:
            rejects.append(f"$scale {match.group(1)} (the merged model is written at "
                           "$scale 1.0: this weapon would change size)")
    for match in _ORIGIN_RE.finditer(qc_text):
        try:
            offset = [float(v) for v in match.groups()]
        except ValueError:
            rejects.append(f"unreadable $origin {' '.join(match.groups())!r}")
            continue
        if any(abs(v) > 1e-6 for v in offset):
            rejects.append(f"$origin {' '.join(match.groups())} (the merged model has no "
                           "$origin: this weapon would move)")
    for match in _FLAGS_RE.finditer(qc_text):
        if match.group(1) not in ("0", "0.0"):
            warnings.append(f"$flags {match.group(1)} dropped (one model-wide value; "
                            "the merged model uses $flags 0)")
    return HeaderCheck(rejects, warnings)


def check_sequences(model: ModelInput) -> HeaderCheck:
    """Sequences merge-v cannot rebuild faithfully: a blend (several
    animation SMDs in one block) would keep only its first animation —
    rejected; other options (origin, rotate, motion extraction LX…) are not
    carried — warned. (``ACT_*`` activities are carried.)"""
    rejects: list[str] = []
    options: dict[str, list[str]] = {}
    for seq in model.sequences:
        if seq.smd_count > 1:
            rejects.append(f"sequence {seq.name!r} blends {seq.smd_count} animations "
                           "(merge-v keeps one)")
        for option in seq.options:
            if option not in ("blend", "animation") or seq.smd_count <= 1:
                options.setdefault(option, []).append(seq.name)
    warnings = [f"sequence option {option!r} not carried ({', '.join(names[:4])}"
                f"{'…' if len(names) > 4 else ''})" for option, names in sorted(options.items())]
    return HeaderCheck(rejects, warnings)


def _remap(smd: Smd, mapping: dict[str, str]) -> Smd:
    clone = smd.clone()
    clone.triangles = [
        Triangle(mapping.get(t.material.lower(), t.material), t.vertices)
        if t.material.lower() in mapping else t
        for t in smd.triangles
    ]
    return clone


def skin_variants(model: ModelInput, parts: ModelParts
                  ) -> list[tuple[ModelInput, ModelParts, int]]:
    """``(variant model, its parts, skin index)`` for every ``$texturegroup``
    row past the first. A variant retextures only the weapon meshes (the
    hands are never skinned); rows that change nothing are skipped."""
    rows = parse_texturegroups(model.qc_text)
    if len(rows) < 2:
        return []
    base = rows[0]
    weapon_stems = {stem for group in parts.weapon_stems for stem in group}
    used = {t.material.lower() for stem in weapon_stems
            for t in model.meshes[stem].triangles}
    out: list[tuple[ModelInput, ModelParts, int]] = []
    for index, row in enumerate(rows[1:], start=1):
        mapping = {old.lower(): new for old, new in zip(base, row, strict=False)
                   if old.lower() != new.lower() and old.lower() in used}
        if not mapping:
            continue
        meshes = {stem: (_remap(smd, mapping) if stem in weapon_stems else smd.clone())
                  for stem, smd in model.meshes.items()}
        variant = ModelInput(
            name=f"{model.name}{SKIN_SUFFIX}{index}", directory=model.directory,
            qc_path=model.qc_path, qc_text=model.qc_text,
            bodygroups=dict(model.bodygroups), sequences=list(model.sequences),
            meshes=meshes, anims={k: v.clone() for k, v in model.anims.items()},
            warnings=[],
        )
        out.append((variant, replace(
            parts, weapon_stems=[list(g) for g in parts.weapon_stems],
            hand_variants=list(parts.hand_variants), dropped=dict(parts.dropped),
            synthetic=set(parts.synthetic), warnings=[]), index))
    return out


__all__ = ["HeaderCheck", "SKIN_SUFFIX", "check_header", "check_sequences",
           "skin_variants"]
