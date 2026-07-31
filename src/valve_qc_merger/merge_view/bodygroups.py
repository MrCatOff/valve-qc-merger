"""Bodygroup collapse for merge-view (spec §3.6).

Per model: switchable bodygroups collapse to their first entry (every kept
group costs one of GoldSource's 32 bodyparts and multiplies ``pev_body``), the
hand group is identified and normalised to a single ``hands`` entry carrying
the model's OWN hand mesh, and the always-on weapon pieces are packed into as
few submodels as the 2048-vertex budget allows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.models.smd import Smd

VERTEX_BUDGET = 2048  # studiomdl's per-submodel vertex cap


@dataclass
class ModelParts:
    """One model reduced to its merged-output pieces."""

    weapon_stems: list[list[str]] = field(default_factory=list)  # per weapon submodel
    hands_stem: str | None = None
    dropped: dict[str, list[str]] = field(default_factory=dict)  # group -> dropped entries
    warnings: list[str] = field(default_factory=list)


def _unique_vertices(smd: Smd) -> int:
    """Approximate studiomdl's vertex count: unique (bone, position) pairs."""
    return len({
        (v.bone, round(v.position.x, 4), round(v.position.y, 4), round(v.position.z, 4))
        for t in smd.triangles for v in t.vertices
    })


def _hand_fraction(smd: Smd) -> float:
    """Fraction of vertices bound to canonical hand bones (post-canonicalise)."""
    total = 0
    hand = 0
    name_of = {n.index: n.name for n in smd.nodes}
    for t in smd.triangles:
        for v in t.vertices:
            total += 1
            if name_of[v.bone].startswith("Bip01"):
                hand += 1
    return hand / total if total else 0.0


def collapse_bodygroups(
    model: ModelInput, *, keep_groups: frozenset[str] = frozenset()
) -> ModelParts:
    """Reduce a canonicalised model to weapon submodels + one hands mesh."""
    parts = ModelParts()
    always_on: list[str] = []

    for group, stems in model.bodygroups.items():
        resolved = [s for s in stems if s in model.meshes]
        if not resolved:
            continue
        is_hand_group = "hand" in group.lower() or (
            _hand_fraction(model.meshes[resolved[0]]) > 0.5
        )
        if is_hand_group:
            if parts.hands_stem is None:
                parts.hands_stem = resolved[0]
            if len(resolved) > 1:
                parts.dropped[group] = resolved[1:]
            continue
        kept = resolved if group in keep_groups else resolved[:1]
        if len(resolved) > len(kept):
            parts.dropped[group] = resolved[len(kept):]
        always_on.extend(kept)

    if parts.hands_stem is None:
        # No named hand group: pick the mesh with the highest canonical-bone
        # vertex share, if any crosses the threshold.
        scored = sorted(
            ((stem, _hand_fraction(smd)) for stem, smd in model.meshes.items()),
            key=lambda item: -item[1],
        )
        if scored and scored[0][1] > 0.5:
            parts.hands_stem = scored[0][0]
            always_on = [s for s in always_on if s != parts.hands_stem]
        else:
            parts.warnings.append("no hand mesh identified; model has no hands entry")

    # Pack always-on pieces into submodels under the vertex budget (greedy,
    # deterministic order as listed by the QC).
    current: list[str] = []
    current_verts = 0
    for stem in always_on:
        verts = _unique_vertices(model.meshes[stem])
        if verts > VERTEX_BUDGET:
            parts.warnings.append(
                f"mesh {stem!r} alone exceeds the {VERTEX_BUDGET}-vertex budget ({verts})"
            )
        if current and current_verts + verts > VERTEX_BUDGET:
            parts.weapon_stems.append(current)
            current, current_verts = [], 0
        current.append(stem)
        current_verts += verts
    if current:
        parts.weapon_stems.append(current)
    if not parts.weapon_stems:
        parts.warnings.append("no weapon meshes after collapse")
    return parts


__all__ = ["ModelParts", "collapse_bodygroups", "VERTEX_BUDGET"]
