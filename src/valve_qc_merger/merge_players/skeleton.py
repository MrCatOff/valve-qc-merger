"""Reduce a CSO body mesh to the bones it actually needs, on donor names.

CSO player rigs are the CS 1.6 ValveBiped skeleton plus a few mesh-specific
sub-bones (Breast_Sub, Eye_Sub, ...), each a child of a core Bip01 bone. Under
the donor's shared animations those sub-bones never move, so we collapse them:
rebind their vertices onto the nearest donor ancestor bone (geometrically exact
— SMD vertex positions are absolute, and a static sub-bone rigidly follows its
parent) and drop the now-empty bones.

We then prune every VERTEX-LESS LEAF bone. studiomdl unions reference bones by
name and prunes globally-vertexless bones at compile time — and that pruning
silently corrupts runtime mesh strips (goldsource studiomdl reindex bug). By
pre-pruning, the emitted skeleton contains only used bones and the internal
ancestors that hold them together; the donor's ~55-bone animations still drive
every surviving bone by name, and dropped leaves (fingers, mouth ``Bone01``,
twist helpers the CSO body never weights) simply never appear.
"""

from __future__ import annotations

from valve_qc_merger.merge_players.discovery import Donor
from valve_qc_merger.merge_view.skeleton_ops import (
    rebind_vertices,
    remove_bones,
    renumber,
)
from valve_qc_merger.models.smd import Smd


class SkeletonError(RuntimeError):
    """A body mesh that cannot be reduced onto the donor skeleton."""


def _depth(name: str, parent_of: dict[str, str | None]) -> int:
    depth, cursor = 0, parent_of.get(name)
    while cursor is not None:
        depth, cursor = depth + 1, parent_of.get(cursor)
    return depth


def _prune_vertexless_leaves(mesh: Smd) -> None:
    """Iteratively drop leaf bones no vertex uses (studiomdl would, destructively)."""
    while True:
        used = {v.bone for t in mesh.triangles for v in t.vertices}
        has_child = {n.parent for n in mesh.nodes}
        doomed = {n.name for n in mesh.nodes
                  if n.index not in has_child and n.index not in used}
        if not doomed:
            return
        remove_bones(mesh, doomed)


def reduce_body(mesh: Smd, donor: Donor) -> list[str]:
    """Collapse foreign bones and prune vertexless leaves (in place).

    A bone is *foreign* if its name is not a donor bone, OR its parent differs
    from the donor's parent for that name — CSO rigs reuse generic names
    (``Bone01``, ``Object04``) for unrelated bones, and keeping one whose
    parentage disagrees with the donor animations makes studiomdl reject the
    compile ("illegal parent bone replacement"). Foreign bones' vertices fold
    onto the nearest non-foreign ancestor (exact: they are static under the
    donor animations). Returns the collapsed bone names.
    """
    donor_names = {name for name, _ in donor.table}
    donor_parent = dict(donor.table)
    name_by_index = {n.index: n.name for n in mesh.nodes}
    parent_of = {n.name: name_by_index.get(n.parent) for n in mesh.nodes}

    def foreign(name: str) -> bool:
        return name not in donor_names or parent_of.get(name) != donor_parent.get(name)

    doomed = [n.name for n in mesh.nodes if foreign(n.name)]
    # Deepest first so a foreign chain folds toward the core without gaps.
    for name in sorted(doomed, key=lambda s: -_depth(s, parent_of)):
        ancestor = parent_of.get(name)
        while ancestor is not None and foreign(ancestor):
            ancestor = parent_of.get(ancestor)
        if ancestor is None:
            raise SkeletonError(
                f"foreign bone {name!r} has no donor ancestor to collapse onto"
            )
        rebind_vertices(mesh, name, ancestor)

    if doomed:
        remove_bones(mesh, set(doomed))  # refuses vertex-carrying bones (rebound above)
    _prune_vertexless_leaves(mesh)
    renumber(mesh)
    return sorted(doomed)


__all__ = ["reduce_body", "SkeletonError"]
