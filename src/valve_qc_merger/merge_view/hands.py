"""Hand rig detection and canonical rename mapping (merge-v M2, spec §3.3).

Reuses the retarget correspondence engine — the same geometric machinery that
already survived the corpus's pathologies (leaf stubs under wrists, reversed
forearms, curled rest poses, synthetic tails, depth-separated arms) — with the
MODEL as the source rig and the REFERENCE hands as the target. Inverting the
resulting bone map yields ``{model bone -> reference name}``.

Matching runs against the reference *including* its Nub tips: a model whose
fingers carry a 4th joint maps that joint onto the Nub name, and the Nub
removal pass then deletes them all uniformly; 3-joint models simply leave the
Nub names unused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.merge_view.skeleton_ops import fk_worlds
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import Smd
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget.correspondence import (
    CorrespondenceError,
    RigBone,
    build_correspondence,
)


@dataclass
class HandMatch:
    """Outcome of matching one model's rig against the reference hands."""

    renames: dict[str, str]  # model bone -> reference name
    held_reference: list[str]  # reference bones with no model counterpart
    warnings: list[str] = field(default_factory=list)


def rig_bones_from_smd(smd: Smd) -> list[RigBone]:
    """Build correspondence rig bones from a parsed SMD's rest skeleton.

    Heads are FK world positions of the first frame. Tails are synthesised as
    the first child's head (leaves get a tiny offset) — the correspondence
    engine never trusts tails for geometry (the synthetic-tail lesson), but the
    dataclass requires them.
    """
    if not smd.frames:
        raise CorrespondenceError("SMD has no skeleton frame")
    worlds = fk_worlds(smd, smd.frames[0])
    name_of = {n.index: n.name for n in smd.nodes}
    first_child: dict[int, int] = {}
    for node in smd.nodes:
        if node.parent >= 0 and node.parent not in first_child:
            first_child[node.parent] = node.index
    bones: list[RigBone] = []
    for node in smd.nodes:
        head = worlds[node.index].translation
        child = first_child.get(node.index)
        if child is not None:
            tail = worlds[child].translation
        else:
            tail = Vector3(head.x, head.y, head.z + 0.01)
        parent = name_of.get(node.parent) if node.parent >= 0 else None
        bones.append(RigBone(node.name, parent, head, tail))
    return bones


def load_reference_rig(reference_smd: Path) -> list[RigBone]:
    """The canonical hand skeleton as correspondence rig bones."""
    return rig_bones_from_smd(parse_smd_file(reference_smd))


def hand_bone_names(meshes: dict[str, Smd], bodygroups: dict[str, list[str]]) -> set[str] | None:
    """Bones weighted by the model's hand bodygroup meshes, or None if unknown.

    The weapon subtree can hang UNDER a wrist (anaconda) and would otherwise
    qualify as a sixth finger chain; restricting arm discovery to hand-weighted
    bones is the same weights signal the retarget pipeline uses.
    """
    hand_stems = {
        stem
        for group, stems in bodygroups.items()
        if "hand" in group.lower()
        for stem in stems
    }
    if not hand_stems:
        return None
    names: set[str] = set()
    for stem, mesh in meshes.items():
        if stem not in hand_stems:
            continue
        name_of = {n.index: n.name for n in mesh.nodes}
        names.update(
            name_of[v.bone] for t in mesh.triangles for v in t.vertices
        )
    return names or None


def match_hands(
    mesh: Smd, reference: list[RigBone], include: set[str] | None = None
) -> HandMatch:
    """Match one model's fullest mesh rig against the reference hands.

    ``include`` restricts arm discovery to hand-weighted bones (see
    :func:`hand_bone_names`); without it every bone is considered. Raises
    :class:`CorrespondenceError` with a model-level diagnostic when the rig
    cannot be matched confidently (arm count mismatch, ambiguous thumb,
    finger-count mismatch) — spec §3.3 mandates a loud abort over a guess.
    """
    model_bones = rig_bones_from_smd(mesh)
    universe = {b.name for b in model_bones}
    if include is None:
        hand_set = universe
    else:
        # Closure: wrists frequently carry no vertex weights (the retarget §5
        # lesson), so every included bone's ancestors join the set. Ancestors
        # cannot fabricate a >=4-chain wrist, only complete broken chains.
        parent_of = {b.name: b.parent for b in model_bones}
        closed = set(include & universe)
        for name in sorted(include & universe):
            cursor = parent_of.get(name)
            while cursor is not None and cursor not in closed:
                closed.add(cursor)
                cursor = parent_of.get(cursor)
        hand_set = closed or universe
    corr = build_correspondence(model_bones, hand_set, reference)
    renames: dict[str, str] = {}
    held: list[str] = []
    for entry in corr.maps:
        if entry.source is None:
            held.append(entry.target)
        else:
            renames[entry.source] = entry.target
    return HandMatch(renames=renames, held_reference=sorted(held),
                     warnings=list(corr.warnings))


def collision_guard(mesh: Smd, renames: dict[str, str]) -> list[str]:
    """Renames that would merge two distinct bones — must be empty to proceed.

    A target name is unsafe when another bone already carries it and is not
    itself renamed away (spec §3.4: abort the model rather than corrupt it).
    """
    existing = {n.name for n in mesh.nodes}
    conflicts: list[str] = []
    for old, new in sorted(renames.items()):
        if new == old:
            continue
        if new in existing and new not in renames:
            conflicts.append(f"{old!r} -> {new!r} (name already taken)")
    targets = sorted(renames.values())
    for a, b in zip(targets, targets[1:], strict=False):
        if a == b:
            conflicts.append(f"two bones both rename to {a!r}")
    return conflicts


__all__ = [
    "HandMatch",
    "collision_guard",
    "load_reference_rig",
    "match_hands",
    "rig_bones_from_smd",
]
