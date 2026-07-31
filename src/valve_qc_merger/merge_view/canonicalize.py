"""Canonical rename application, re-hierarchy, Nub removal and prune (spec §3.4–3.5).

Applies one model's :class:`~valve_qc_merger.merge_view.hands.HandMatch` to
every SMD (meshes and animations alike) and the QC text, then reshapes the
skeleton onto the reference structure:

1. rename matched bones to the canonical names (QC bone references patched),
2. ensure the shared ``Bip01`` root exists and every orphan root hangs under it,
3. enforce the reference parentage for the canonical subtree, top-down — this
   is what puts reversed-hierarchy rigs (forearm as a child of the hand) right,
4. delete every ``*Nub`` bone (vertices rebound to the parent first, warned),
5. full prune: every vertex-less bone not in the keep-set is folded away
   (canonical hand bones and QC-referenced bones are always kept).

Every structural edit is FK-exact; :func:`verify_pose_preserved` proves it per
model by comparing world positions of surviving bones across all frames of all
SMDs before and after.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.merge_view.hands import HandMatch
from valve_qc_merger.merge_view.skeleton_ops import (
    ensure_root,
    fk_worlds,
    graft_bone,
    rebind_vertices,
    remove_bones,
    rename_bones,
    reparent_bone,
)
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import Node, Smd
from valve_qc_merger.retarget.qc_build import parse_attachments

_ROOT = "Bip01"


@dataclass
class CanonicalReport:
    """What canonicalisation did to one model."""

    renamed: int = 0
    grafted: list[str] = field(default_factory=list)
    reparented: list[str] = field(default_factory=list)
    nubs_removed: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    max_pose_deviation: float = 0.0
    warnings: list[str] = field(default_factory=list)


def _reference_parents(reference_nodes: list[Node]) -> list[tuple[str, str | None]]:
    """(bone, expected parent) for the canonical subtree, parents before children."""
    name_of = {n.index: n.name for n in reference_nodes}
    ordered = sorted(reference_nodes, key=lambda n: n.index)
    return [
        (n.name, name_of.get(n.parent) if n.parent >= 0 else None)
        for n in ordered
        if not n.name.endswith("Nub")  # Nubs are deleted, never enforced
    ]


def _snapshot(smd: Smd) -> list[dict[str, Vector3]]:
    """World head positions by bone name, per frame."""
    name_of = {n.index: n.name for n in smd.nodes}
    out: list[dict[str, Vector3]] = []
    for frame in smd.frames:
        worlds = fk_worlds(smd, frame)
        out.append({name_of[i]: t.translation for i, t in worlds.items()})
    return out


def _qc_kept_bones(model: ModelInput) -> set[str]:
    """Bones the QC references and studiomdl will still need (attachments)."""
    every = {n.name for mesh in model.meshes.values() for n in mesh.nodes}
    kept: set[str] = set()
    for line in parse_attachments(model.qc_text, every):
        # line: $attachment N "bone" x y z
        kept.add(line.split('"')[1])
    return kept


def canonicalize_model(
    model: ModelInput,
    match: HandMatch,
    reference_nodes: list[Node],
    *,
    prune: bool = True,
) -> CanonicalReport:
    """Apply the hand match to every SMD and the QC, in place."""
    report = CanonicalReport()
    renames = {old: new for old, new in match.renames.items() if old != new}
    report.renamed = len(renames)

    # Pose-preservation baseline, keyed by the POST-rename names.
    baselines = {
        key: [
            {renames.get(name, name): pos for name, pos in frame.items()}
            for frame in _snapshot(smd)
        ]
        for key, smd in {**model.meshes, **model.anims}.items()
    }

    for old, new in sorted(renames.items()):
        model.qc_text = model.qc_text.replace(f'"{old}"', f'"{new}"')

    plan = _reference_parents(reference_nodes)
    keep = {name for name, _ in plan} | _qc_kept_bones(model)

    ref_parent = dict(plan)
    for smd in {**model.meshes, **model.anims}.values():
        rename_bones(smd, renames)
        ensure_root(smd, _ROOT)
        present = {n.name for n in smd.nodes}
        # Complete the canonical subtree: graft bones this rig lacks (frozen,
        # vertex-less) so every model agrees on the merged table's parentage.
        for bone, expected in plan:
            if bone in present or expected is None:
                continue
            graft_bone(smd, bone, expected)  # plan is parents-first
            present.add(bone)
            if bone not in report.grafted:
                report.grafted.append(bone)
        parent_of = {n.name: next(
            (p.name for p in smd.nodes if p.index == n.parent), None)
            for n in smd.nodes}
        for bone, expected in plan:
            if bone == _ROOT or bone not in present:
                continue
            # Rigs may lack intermediate canonical bones (no forearm at all):
            # parent under the nearest reference ancestor that IS present.
            target = expected
            while target is not None and target not in present:
                target = ref_parent.get(target)
            if target is None:
                target = _ROOT
            if parent_of.get(bone) != target:
                reparent_bone(smd, bone, target)
                parent_of = {n.name: next(
                    (p.name for p in smd.nodes if p.index == n.parent), None)
                    for n in smd.nodes}
                if bone not in report.reparented:
                    report.reparented.append(bone)

        nubs = {n.name for n in smd.nodes if n.name.endswith("Nub")}
        if nubs:
            used = {v.bone for t in smd.triangles for v in t.vertices}
            name_of = {n.index: n.name for n in smd.nodes}
            parent_name = {n.name: name_of.get(n.parent, _ROOT) for n in smd.nodes}
            for nub in sorted(nubs):
                index = next(n.index for n in smd.nodes if n.name == nub)
                if index in used:
                    moved = rebind_vertices(smd, nub, parent_name[nub] or _ROOT)
                    report.warnings.append(
                        f"{nub}: {moved} vertices rebound to its parent before removal"
                    )
            remove_bones(smd, nubs)
            for nub in sorted(nubs):
                if nub not in report.nubs_removed:
                    report.nubs_removed.append(nub)

        if prune:
            used_names = {
                next(n.name for n in smd.nodes if n.index == v.bone)
                for t in smd.triangles for v in t.vertices
            }
            # Vertex-carrying bones from EVERY mesh stay in every SMD so the
            # model's node tables remain merge-consistent.
            mesh_used = {
                next(n.name for n in m.nodes if n.index == v.bone)
                for m in model.meshes.values()
                for t in m.triangles for v in t.vertices
            }
            doomed = {
                n.name for n in smd.nodes
                if n.name not in keep
                and n.name not in mesh_used
                and n.name not in used_names
            }
            if doomed:
                remove_bones(smd, doomed)
                for name in sorted(doomed):
                    if name not in report.pruned:
                        report.pruned.append(name)

    report.max_pose_deviation = verify_pose_preserved(model, baselines)
    return report


def verify_pose_preserved(
    model: ModelInput, baselines: dict[str, list[dict[str, Vector3]]]
) -> float:
    """Max world-position deviation of surviving bones vs the baseline."""
    worst = 0.0
    for key, smd in {**model.meshes, **model.anims}.items():
        for frame_index, frame in enumerate(smd.frames):
            name_of = {n.index: n.name for n in smd.nodes}
            worlds = fk_worlds(smd, frame)
            baseline = baselines[key][frame_index]
            for index, transform in worlds.items():
                name = name_of[index]
                expected = baseline.get(name)
                if expected is None:
                    continue  # inserted root
                got = transform.translation
                deviation = max(
                    abs(got.x - expected.x), abs(got.y - expected.y),
                    abs(got.z - expected.z),
                )
                worst = max(worst, deviation)
    return worst


__all__ = ["CanonicalReport", "canonicalize_model", "verify_pose_preserved"]
