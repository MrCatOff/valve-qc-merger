"""Automatic bone correspondence between a weapon's hand rig and the reference.

The reference hands (``male.smd``/``female.smd``) always use the same Biped
naming (``Bip01_{L,R}_Hand``, ``Bip01_{L,R}_Finger0..4``). Each weapon's hand
rig is a differently-sized Biped export that keeps two reliable signals:

* the wrist bones are named ``Bone_Lefthand`` / ``Bone_Righthand``;
* every finger is a clean three-bone chain hanging off the wrist.

This module pairs the wrists by name, discovers the weapon's finger chains
structurally, and assigns each to a reference finger (thumb..pinky) by bind-pose
geometry -- comparing finger-root directions in the wrist's local frame.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations

from valve_qc_merger.kinematics import world_transforms
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import Node, Smd
from valve_qc_merger.transform import Transform

# Reference (target) rig names, per side. Fingers are thumb..pinky (0..4).
_TARGET_WRIST = {"L": "Bip01_L_Hand", "R": "Bip01_R_Hand"}
_TARGET_WRIST_SOURCE = {"L": "Bone_Lefthand", "R": "Bone_Righthand"}
FINGER_COUNT = 5
FINGER_JOINTS = 3


@dataclass(frozen=True, slots=True)
class FingerChain:
    """A three-bone finger chain: root, middle and tip bone indices."""

    joints: tuple[int, int, int]

    @property
    def root(self) -> int:
        return self.joints[0]

    @property
    def tip(self) -> int:
        return self.joints[2]


@dataclass(frozen=True, slots=True)
class HandLink:
    """The resolved correspondence for one hand (left or right)."""

    side: str
    source_wrist: int
    target_wrist: int
    finger_pairs: tuple[tuple[FingerChain, FingerChain], ...]


class CorrespondenceError(ValueError):
    """Raised when the weapon rig cannot be matched to the reference rig."""


def _children_map(nodes: list[Node]) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {node.index: [] for node in nodes}
    for node in nodes:
        if node.parent in children:
            children[node.parent].append(node.index)
    return children


def _index_by_name(nodes: list[Node], name: str) -> int | None:
    for node in nodes:
        if node.name == name:
            return node.index
    return None


def _descendants(root: int, children: dict[int, list[int]]) -> list[int]:
    result: list[int] = []
    stack = list(children.get(root, []))
    while stack:
        current = stack.pop()
        result.append(current)
        stack.extend(children.get(current, []))
    return result


def _find_finger_chains(wrist: int, children: dict[int, list[int]]) -> list[FingerChain]:
    """Find clean three-bone chains (root->mid->tip leaf) under ``wrist``.

    A chain qualifies when the tip is a leaf and both the tip's parent and the
    root have exactly one child -- which selects fingers while rejecting the
    palm, weapon mechanism and other branching bones.
    """
    chains: list[FingerChain] = []
    for candidate in _descendants(wrist, children):
        kids = children.get(candidate, [])
        if len(kids) != 1:
            continue
        mid = kids[0]
        mid_kids = children.get(mid, [])
        if len(mid_kids) != 1:
            continue
        tip = mid_kids[0]
        if children.get(tip):
            continue  # tip must be a leaf
        chains.append(FingerChain((candidate, mid, tip)))
    return chains


def _target_finger_chains(nodes: list[Node], side: str) -> list[FingerChain]:
    chains: list[FingerChain] = []
    for finger in range(FINGER_COUNT):
        base = f"Bip01_{side}_Finger{finger}"
        names = [base, f"{base}1", f"{base}2"]
        indices = [_index_by_name(nodes, name) for name in names]
        if any(index is None for index in indices):
            raise CorrespondenceError(f"reference rig is missing finger bone: {names}")
        chains.append(FingerChain((indices[0], indices[1], indices[2])))  # type: ignore[arg-type]
    return chains


def _root_directions(
    chains: list[FingerChain],
    wrist: int,
    world: dict[int, Transform],
) -> list[Vector3]:
    """Unit direction from wrist to each finger root, in the wrist's local frame."""
    wrist_inv = world[wrist].inverse()
    directions: list[Vector3] = []
    for chain in chains:
        local = wrist_inv.transform_point(world[chain.root].translation)
        length = local.length()
        if length == 0:
            directions.append(local)
        else:
            directions.append(Vector3(local.x / length, local.y / length, local.z / length))
    return directions


def _best_assignment(
    source_dirs: list[Vector3], target_dirs: list[Vector3]
) -> tuple[int, ...]:
    """Return the permutation ``p`` mapping target i -> source p[i] minimising angle."""
    best_perm: tuple[int, ...] = tuple(range(len(source_dirs)))
    best_cost = float("inf")
    for perm in permutations(range(len(source_dirs))):
        cost = 0.0
        for target_index, source_index in enumerate(perm):
            a = target_dirs[target_index]
            b = source_dirs[source_index]
            dot = a.x * b.x + a.y * b.y + a.z * b.z
            cost += 1.0 - dot  # 0 when aligned, up to 2 when opposed
        if cost < best_cost:
            best_cost = cost
            best_perm = perm
    return best_perm


def _build_side(source: Smd, target: Smd, side: str) -> HandLink:
    source_children = _children_map(source.nodes)
    source_wrist = _index_by_name(source.nodes, _TARGET_WRIST_SOURCE[side])
    target_wrist = _index_by_name(target.nodes, _TARGET_WRIST[side])
    if source_wrist is None:
        raise CorrespondenceError(f"weapon rig has no {_TARGET_WRIST_SOURCE[side]!r} bone")
    if target_wrist is None:
        raise CorrespondenceError(f"reference rig has no {_TARGET_WRIST[side]!r} bone")

    source_chains = _find_finger_chains(source_wrist, source_children)
    if len(source_chains) != FINGER_COUNT:
        raise CorrespondenceError(
            f"expected {FINGER_COUNT} finger chains under {_TARGET_WRIST_SOURCE[side]!r}, "
            f"found {len(source_chains)}"
        )
    target_chains = _target_finger_chains(target.nodes, side)

    source_world = world_transforms(source.nodes, source.frames[0])
    target_world = world_transforms(target.nodes, target.frames[0])
    source_dirs = _root_directions(source_chains, source_wrist, source_world)
    target_dirs = _root_directions(target_chains, target_wrist, target_world)

    perm = _best_assignment(source_dirs, target_dirs)
    finger_pairs = tuple(
        (source_chains[perm[target_index]], target_chains[target_index])
        for target_index in range(FINGER_COUNT)
    )
    return HandLink(side, source_wrist, target_wrist, finger_pairs)


def build_hand_correspondences(source: Smd, target: Smd) -> list[HandLink]:
    """Resolve left- and right-hand correspondence between weapon and reference.

    ``source`` is the weapon reference SMD (whole skeleton, bind pose in frame 0);
    ``target`` is the reference hand SMD. Raises :class:`CorrespondenceError`
    when either rig does not match the expected structure.
    """
    if not source.frames or not target.frames:
        raise CorrespondenceError("both rigs need a bind-pose skeleton frame")
    return [_build_side(source, target, side) for side in ("L", "R")]


__all__ = [
    "CorrespondenceError",
    "FingerChain",
    "HandLink",
    "build_hand_correspondences",
]
