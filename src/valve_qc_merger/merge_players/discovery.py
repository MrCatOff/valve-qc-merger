"""Loading for merge-players: donor rig + lightweight CSO body models.

merge-p's ``load_model`` eagerly parses every animation SMD a QC references —
fatal here, where CSO models ship hundreds of anims we discard. This loader
parses only each model's primary *body* mesh (its skin), plus the hitbox block
and mesh height used for grouping. The donor is loaded separately: its
reference SMD (for the canonical node table + bind pose) and its QC text (for
the sequence set, hitboxes, attachments, controller); its animation SMDs are
copied as files, never parsed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.merge_view.discovery import MergeViewError
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import Node, Smd
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget.qc_build import parse_bodygroups

# Bodygroup names that are accessories, not the character's primary body.
_ACCESSORY_HINTS = ("backpack", "bomb", "c4", "xmas", "cap", "hat", "back", "bag")
_HBOX_RE = re.compile(r"^\s*\$hbox\b.*$", re.MULTILINE)

# Core bones whose bind lengths define a rig's proportions (the stretch axis).
_SIG_BONES = (
    "Bip01 Spine", "Bip01 Spine1", "Bip01 Spine2", "Bip01 Neck", "Bip01 Head",
    "Bip01 L Thigh", "Bip01 L Calf", "Bip01 L Foot",
    "Bip01 L UpperArm", "Bip01 L Forearm", "Bip01 R UpperArm", "Bip01 R Forearm",
)


@dataclass
class PlayerModel:
    """One CSO player model reduced to its skin body + grouping signature.

    Carries ``name``/``directory``/``qc_text`` so the shared merge-view texture
    staging helpers (which key on those attributes) accept it directly.
    """

    name: str
    directory: Path
    qc_path: Path
    qc_text: str
    body_meshes: list[Smd]  # the primary body studio SMD(s), concatenated at emit
    body_stems: list[str]
    hitbox_sig: str  # hash of the $hbox block
    proportion_sig: str  # quantised core-bone lengths — the stretch/size class
    height: float  # vertical extent of the body mesh (z), informational
    warnings: list[str] = field(default_factory=list)


@dataclass
class Donor:
    """The canonical CS 1.6 rig donor (arctic)."""

    directory: Path
    qc_path: Path
    qc_text: str
    reference: Smd  # the donor body reference SMD (skeleton table + bind)
    body_stem: str
    table: list[tuple[str, str | None]]  # (name, parent-name), donor node order
    bind: dict[str, tuple[object, object]]  # name -> (position, rotation) at frame 0


def _hitbox_sig(qc_text: str) -> str:
    block = "\n".join(m.group(0).strip() for m in _HBOX_RE.finditer(qc_text))
    return hashlib.md5(block.encode("latin-1", "replace")).hexdigest()[:8]


def _mesh_height(mesh: Smd) -> float:
    box = mesh.bounding_box()
    return float(box.maxs.z - box.mins.z) if box is not None else 0.0


def bind_positions(mesh: Smd) -> dict[str, Vector3]:
    """Bind-pose (frame 0) local position per bone name."""
    if not mesh.frames:
        return {}
    name_of = {n.index: n.name for n in mesh.nodes}
    return {name_of[p.bone]: p.position for p in mesh.frames[0].poses}


def _proportion_sig(mesh: Smd) -> str:
    """Quantised core-bone lengths — models with the same value share a skeleton.

    The stretch is a proportion (bone-length) mismatch, so this is the correct
    axis to group on: two rigs with the same signature can share one animation
    set with no stretch; different signatures must not.
    """
    bp = bind_positions(mesh)
    parts: list[str] = []
    for bone in _SIG_BONES:
        p = bp.get(bone)
        length = round((p.x * p.x + p.y * p.y + p.z * p.z) ** 0.5) if p else 0
        parts.append(str(length))
    return "-".join(parts)


def _body_stems(bodygroups: dict[str, list[str]]) -> list[str]:
    """The character body: first studio of EACH non-accessory bodygroup.

    Every ``$bodygroup`` is a separate bodypart that GoldSrc renders
    simultaneously, so a body split across parts — torso, legs, head — needs one
    studio from EACH. The decompiler names duplicate blocks ``studio``/
    ``studio_2``; those are still separate parts (pirategirl = torso + legs), not
    variants, so both are taken. Multiple studios WITHIN one block are switchable
    variants — only the first (default) is taken.
    """
    stems: list[str] = []
    for group, group_stems in bodygroups.items():
        low = group.lower()
        if any(hint in low for hint in _ACCESSORY_HINTS) or not group_stems:
            continue
        stems.append(group_stems[0])
    return stems


def _primary_body_stems(bodygroups: dict[str, list[str]]) -> list[str]:
    stems = _body_stems(bodygroups)
    if stems:
        return stems
    for group_stems in bodygroups.values():  # last resort: first studio anywhere
        if group_stems:
            return [group_stems[0]]
    return []


def _resolve_smd(model_dir: Path, stem: str) -> Path:
    relative = stem.replace("\\", "/")
    if not relative.lower().endswith(".smd"):
        relative += ".smd"
    return model_dir / relative


def _ascii_key(text: str) -> str:
    """Drop non-ASCII (mojibake) chars and lowercase — for tolerant matching."""
    return "".join(c for c in text if c.isascii() and c.isprintable()).strip().lower()


def _resolve_materials(meshes: list[Smd], directory: Path) -> list[str]:
    """Rewrite each mesh material to a real on-disk texture file; report the misses.

    CSO decompiles often prefix a material with mojibake (``іЄЕё»юCopyright...``)
    while the file on disk lacks it. An exact-then-ASCII-stripped match repairs
    that so studiomdl's ``$cliptotextures`` finds the BMP (a missing texture
    SIGTRAPs the compiler, not a clean error). Returns unresolved material names.
    """
    files = [p.name for p in directory.iterdir() if p.is_file()]
    exact = {f.lower(): f for f in files}
    exact |= {f.rsplit(".", 1)[0].lower(): f for f in files}
    ascii_map = {_ascii_key(f): f for f in files}
    ascii_map |= {_ascii_key(f.rsplit(".", 1)[0]): f for f in files}

    remap: dict[str, str] = {}
    missing: list[str] = []
    for material in {t.material for mesh in meshes for t in mesh.triangles}:
        m = material.lower()
        hit = (exact.get(m) or exact.get(m.removesuffix(".bmp"))
               or ascii_map.get(_ascii_key(material))
               or ascii_map.get(_ascii_key(material.removesuffix(".bmp"))))
        if hit is None:
            missing.append(material)
        elif hit != material:
            remap[material] = hit
    if remap:
        for mesh in meshes:
            mesh.triangles = [
                dataclasses.replace(t, material=remap.get(t.material, t.material))
                for t in mesh.triangles
            ]
    return sorted(missing)


def load_player_body(model_dir: Path) -> PlayerModel:
    """Parse one CSO model's QC and its primary body mesh only (no anims)."""
    qc_path = sorted(model_dir.glob("*.qc"))[0]
    qc_text = qc_path.read_text(encoding="latin-1")
    bodygroups = parse_bodygroups(qc_text)
    stems = _primary_body_stems(bodygroups)
    warnings: list[str] = []
    meshes: list[Smd] = []
    for stem in stems:
        path = _resolve_smd(model_dir, stem)
        if not path.exists():
            warnings.append(f"body studio missing: {path.name}")
            continue
        meshes.append(parse_smd_file(path))
    if not meshes:
        raise MergeViewError(f"model {model_dir.name!r}: no body mesh resolved from the QC")
    missing = _resolve_materials(meshes, model_dir)
    if missing:
        # A missing texture SIGTRAPs studiomdl under $cliptotextures — skip the
        # whole model rather than emit one that cannot compile.
        raise MergeViewError(
            f"model {model_dir.name!r}: texture(s) not found on disk: "
            f"{', '.join(missing)}"
        )
    accessories = [g for g in bodygroups
                   if any(h in g.lower() for h in _ACCESSORY_HINTS)]
    if accessories:
        warnings.append(f"dropped accessory bodygroups: {', '.join(accessories)}")
    fullest = max(meshes, key=lambda m: len(m.nodes))
    return PlayerModel(
        name=model_dir.name, directory=model_dir, qc_path=qc_path, qc_text=qc_text,
        body_meshes=meshes, body_stems=stems,
        hitbox_sig=_hitbox_sig(qc_text), proportion_sig=_proportion_sig(fullest),
        height=round(_mesh_height(meshes[0]), 2), warnings=warnings,
    )


def load_donor(donor_dir: Path) -> Donor:
    """Load the canonical rig donor: reference SMD + QC text.

    The reference SMD (largest non-animation body studio) provides the node
    table and bind pose the merged skeleton conforms to.
    """
    qcs = sorted(donor_dir.glob("*.qc"))
    if not qcs:
        raise MergeViewError(f"donor {donor_dir} has no .qc")
    qc_path = qcs[0]
    qc_text = qc_path.read_text(encoding="latin-1")
    bodygroups = parse_bodygroups(qc_text)
    stems = _primary_body_stems(bodygroups)
    if not stems:
        raise MergeViewError(f"donor {donor_dir.name!r}: no body studio in the QC")
    ref = parse_smd_file(_resolve_smd(donor_dir, stems[0]))
    if not ref.frames:
        raise MergeViewError(f"donor reference {stems[0]!r} has no bind frame")
    name_of: dict[int, str] = {n.index: n.name for n in ref.nodes}
    table: list[tuple[str, str | None]] = [
        (n.name, name_of.get(n.parent) if n.parent >= 0 else None) for n in ref.nodes
    ]
    bind: dict[str, tuple[object, object]] = {
        name_of[p.bone]: (p.position, p.rotation) for p in ref.frames[0].poses
    }
    return Donor(
        directory=donor_dir, qc_path=qc_path, qc_text=qc_text,
        reference=ref, body_stem=stems[0], table=table, bind=bind,
    )


def donor_body_mesh(donor: Donor) -> Smd:
    """A copy of the donor body mesh (already on the donor node table)."""
    ref = donor.reference
    return Smd(version=ref.version, nodes=[Node(n.index, n.name, n.parent) for n in ref.nodes],
               frames=list(ref.frames), triangles=list(ref.triangles))


__all__ = ["PlayerModel", "Donor", "load_player_body", "load_donor", "donor_body_mesh"]
