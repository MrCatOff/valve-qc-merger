"""Skeleton unification planning (Phase 5, §7.7), pure Python and ``bpy``-free.

Given the source rig (``BoneNN``), the hand/weapon bone classification and the
reference→source bone correspondence, decide **which weapon bones form each gun
subtree** and **which reference wrist each gun attaches to**. The in-Blender
worker then appends those bones to the reference armature and re-parents each gun
root under the assigned reference wrist.

A gun subtree is the maximal set of bones rooted at a *boundary* bone — a
non-hand bone whose parent is a hand bone — together with all of its descendants.
On v_elite each gun hangs off its arm's forearm, so the gun root's parent is a
hand bone even though the wrist itself is a sibling subtree; assignment therefore
matches the gun to the arm whose wrist shares an ancestor with the gun root, not
to a wrist on the gun's own ancestor chain.
"""

from __future__ import annotations

from dataclasses import dataclass

from valve_qc_merger.retarget.correspondence import Correspondence, RigBone


@dataclass(frozen=True)
class GunSubtree:
    """One gun: its root bone, every bone in the subtree (parents before children),
    and the source bone its root hangs off (always a hand bone)."""

    root: str
    bones: tuple[str, ...]
    src_parent: str


class UnifyError(RuntimeError):
    """Gun-subtree discovery or arm→gun assignment failed (maps to exit 3)."""


def _parent_map(src_bones: list[RigBone]) -> dict[str, str | None]:
    return {b.name: b.parent for b in src_bones}


def _children_map(src_bones: list[RigBone]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {b.name: [] for b in src_bones}
    for bone in src_bones:
        if bone.parent in children:
            children[bone.parent].append(bone.name)
    for kids in children.values():
        kids.sort()
    return children


def _subtree(root: str, children: dict[str, list[str]]) -> tuple[str, ...]:
    """Depth-first bones of ``root``'s subtree, parents before children."""
    order: list[str] = []
    stack = [root]
    while stack:
        name = stack.pop()
        order.append(name)
        # push reversed so the sorted children are visited in order
        stack.extend(reversed(children.get(name, [])))
    return tuple(order)


def discover_guns(
    src_bones: list[RigBone], hand_set: set[str], weapon_set: set[str]
) -> list[GunSubtree]:
    """Find every gun subtree (§7.7, §5 gun-subtree→arm bijection input).

    A gun root is a non-hand bone whose parent is a hand bone; its subtree must
    contain at least one weapon-weighted bone (guards against stray non-hand
    bones that carry no weapon geometry).
    """
    parent = _parent_map(src_bones)
    children = _children_map(src_bones)
    guns: list[GunSubtree] = []
    for bone in src_bones:
        p = parent.get(bone.name)
        if bone.name in hand_set or p is None or p not in hand_set:
            continue
        bones = _subtree(bone.name, children)
        if not any(name in weapon_set for name in bones):
            continue
        guns.append(GunSubtree(bone.name, bones, p))
    guns.sort(key=lambda g: g.root)
    return guns


def _ancestors(name: str, parent: dict[str, str | None]) -> list[str]:
    chain: list[str] = []
    cursor = parent.get(name)
    while cursor is not None:
        chain.append(cursor)
        cursor = parent.get(cursor)
    return chain


def assign_wrists(
    guns: list[GunSubtree], src_bones: list[RigBone], corr: Correspondence
) -> dict[str, str]:
    """Map each gun root to a reference wrist bone (§7.7 arm→gun map).

    For each source wrist (the source bone a reference wrist maps to) collect its
    ancestor chain. A gun belongs to the arm whose wrist shares the gun root's
    parent as a wrist-or-ancestor; ties break to the deepest (nearest) wrist. The
    result is the reference wrist bone name to parent the gun root under.
    """
    parent = _parent_map(src_bones)
    mapping = corr.as_dict()
    # reference wrist target -> source wrist bone
    wrist_pairs: list[tuple[str, str]] = []
    for m in corr.maps:
        src_wrist = mapping.get(m.target)
        if m.role == "wrist" and src_wrist is not None:
            wrist_pairs.append((m.target, src_wrist))
    if not wrist_pairs:
        raise UnifyError("no mapped wrist to attach a gun to")

    # source wrist -> its ancestor set (including itself), with depth for tie-break
    wrist_reach: list[tuple[str, str, set[str]]] = []  # (ref_target, src_wrist, reach)
    for ref_target, src_wrist in wrist_pairs:
        reach = {src_wrist, *_ancestors(src_wrist, parent)}
        wrist_reach.append((ref_target, src_wrist, reach))

    assignment: dict[str, str] = {}
    for gun in guns:
        candidates = [
            (len(reach), ref_target)
            for ref_target, _src_wrist, reach in wrist_reach
            if gun.src_parent in reach
        ]
        if not candidates:
            raise UnifyError(
                f"gun {gun.root} (off {gun.src_parent}) matches no arm's wrist chain"
            )
        # deepest wrist chain (smallest reach set) wins — the nearest arm
        candidates.sort()
        assignment[gun.root] = candidates[0][1]

    targets = list(assignment.values())
    if len(set(targets)) != len(targets):
        raise UnifyError(f"arm→gun map is not a bijection: {assignment}")
    return assignment


__all__ = ["GunSubtree", "UnifyError", "discover_guns", "assign_wrists"]
