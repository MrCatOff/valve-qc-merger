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
    """Put a body mesh on the donor skeleton (in place).

    1. A bone whose NAME is not a donor bone is *foreign*: its vertices fold
       onto the nearest donor-named ancestor (exact: it is static under the
       donor animations) — or onto the donor root when it hangs off no donor
       bone (an effect bone ``fx02`` at the root, a ``Scene Root`` above
       ``Bip01``) — and it is dropped.
    2. The rest is conformed to the donor's node table: a donor bone the body
       lacks is grafted at the donor's bind, and a donor-named bone hanging off
       another parent (``Bip01 Head`` under ``Spine`` with no ``Neck``) is
       re-parented as the donor has it, world poses kept exactly. Dropping
       such a bone instead re-hung its children further up, against the donor
       animations ("illegal parent bone replacement").
    3. Vertex-less leaves are pruned (studiomdl's own pruning corrupts mesh
       strips).

    Returns the collapsed (foreign) bone names."""
    from valve_qc_merger.merge_view.skeleton_ops import conform_to_table
    donor_names = {name for name, _ in donor.table}
    root = next(name for name, parent in donor.table if parent is None)
    name_by_index = {n.index: n.name for n in mesh.nodes}
    parent_of = {n.name: name_by_index.get(n.parent) for n in mesh.nodes}
    used = {name_by_index[v.bone] for t in mesh.triangles for v in t.vertices}

    doomed = [n.name for n in mesh.nodes if n.name not in donor_names]
    for name in sorted(doomed, key=lambda s: -_depth(s, parent_of)):
        ancestor = parent_of.get(name)
        while ancestor is not None and ancestor not in donor_names:
            ancestor = parent_of.get(ancestor)
        if ancestor is None:
            if name not in used:
                continue  # nothing to keep: the bone just goes
            if root not in parent_of:
                _graft_root(mesh, root, donor)
                parent_of[root] = None
            ancestor = root
        rebind_vertices(mesh, name, ancestor)
        used.add(ancestor)

    if doomed:
        remove_bones(mesh, set(doomed))  # refuses vertex-carrying bones (rebound above)
    try:
        conform_to_table(mesh, list(donor.table), dict(donor.bind))
    except (KeyError, ValueError) as exc:
        raise SkeletonError(f"cannot fit the donor skeleton: {exc}") from exc
    _prune_vertexless_leaves(mesh)
    renumber(mesh)
    return sorted(doomed)


def _graft_root(mesh: Smd, root: str, donor: Donor) -> None:
    """Add the donor root bone at its bind (for vertices of a foreign root)."""
    from valve_qc_merger.models.smd import BonePose, Frame, Node
    index = max((n.index for n in mesh.nodes), default=-1) + 1
    mesh.nodes = [*mesh.nodes, Node(index, root, -1)]
    pos, rot = donor.bind[root]
    mesh.frames = [Frame(f.time, (*f.poses, BonePose(index, pos, rot)))
                   for f in mesh.frames]


__all__ = ["reduce_body", "SkeletonError"]
