"""Bodygroup collapse for merge-v (spec §3.6).

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
    # Every entry of the hand bodygroup (e.g. [female, male]), QC order. The
    # per-weapon merge keeps only ``hands_stem``; the shared-hands merge keeps
    # all variants as ONE shared bodygroup across the whole merged model.
    hand_variants: list[str] = field(default_factory=list)
    dropped: dict[str, list[str]] = field(default_factory=dict)  # group -> dropped entries
    warnings: list[str] = field(default_factory=list)
    # stems created in memory (a folded multi-part weapon) with no SMD on disk
    synthetic: set[str] = field(default_factory=set)
    fold_report: object | None = None  # decimate.FoldReport when parts were folded


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
            if "Bip01" in name_of[v.bone]:
                hand += 1
    return hand / total if total else 0.0


FOLDED_STEM = "__folded_weapon"


def collapse_bodygroups(
    model: ModelInput, *, keep_groups: frozenset[str] = frozenset(),
    max_decimation: float = 0.0,
) -> ModelParts:
    """Reduce a canonicalised model to weapon submodels + one hands mesh.

    With ``max_decimation > 0``, always-on parts that do not fit ONE
    submodel are folded into one anyway when removing at most that fraction
    of their vertices (seam/bone/boundary-safe half-edge collapses) brings
    them under the budget (see :mod:`.decimate`); the folded mesh is added
    to ``model.meshes`` as :data:`FOLDED_STEM`."""
    parts = ModelParts()
    always_on: list[str] = []

    for group, stems in model.bodygroups.items():
        resolved = [s for s in stems if s in model.meshes]
        if not resolved:
            continue
        # The bodygroup NAME is authoritative when it names a role. An
        # explicit "weapon" group is never hands, even when most of its verts
        # sit on "Bip01 ..."-named bones: CSO weapon rigs reuse the Bip01
        # prefix for creature/prop bones (e.g. v_heavyzg's "Bip01 Head",
        # "Bip01 Spine2"), which the vertex-fraction fallback would otherwise
        # mistake for hands. Fall back to the fraction test only when the name
        # gives no signal.
        lname = group.lower()
        if "weapon" in lname:
            is_hand_group = False
        elif "hand" in lname:
            is_hand_group = True
        else:
            is_hand_group = _hand_fraction(model.meshes[resolved[0]]) > 0.5
        if is_hand_group:
            if parts.hands_stem is None:
                parts.hands_stem = resolved[0]
                parts.hand_variants = list(resolved)  # all variants (female, male)
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
    if len(parts.weapon_stems) > 1 and max_decimation > 0:
        from valve_qc_merger.merge_view.decimate import fold_parts
        stems = [stem for group in parts.weapon_stems for stem in group]
        folded = fold_parts([model.meshes[s] for s in stems], budget=VERTEX_BUDGET,
                            max_fraction=max_decimation)
        if folded is not None:
            mesh, report = folded
            model.meshes[FOLDED_STEM] = mesh
            parts.weapon_stems = [[FOLDED_STEM]]
            parts.synthetic.add(FOLDED_STEM)
            parts.fold_report = report
            parts.warnings.append(
                f"folded {len(stems)} parts into one submodel: {report.vertices_before} -> "
                f"{report.vertices_after} vertices (-{report.removed_fraction:.1%}), "
                f"surface error <= {report.surface_error:.3f}u")
    if not parts.weapon_stems:
        parts.warnings.append("no weapon meshes after collapse")
    return parts


__all__ = ["ModelParts", "collapse_bodygroups", "VERTEX_BUDGET"]
