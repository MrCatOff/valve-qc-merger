"""Automatic bone correspondence between a weapon's hand rig and the reference.

The reference hands (``male.smd``/``female.smd``) always use the same Biped
naming (``Bip01_{L,R}_Hand``, ``Bip01_{L,R}_Finger0..4``). Each weapon's hand
rig is a differently-sized Biped export where every finger is a clean three-bone
chain hanging off a wrist bone.

Wrists are resolved two ways: by name (``Bone_Lefthand`` / ``Bone_Righthand``)
when the rig uses that convention, otherwise structurally -- a wrist is a bone
that parents five finger chains. Left/right is then assigned by name when known,
or by picking the handedness whose finger fan best matches the reference (a left
hand matches a left hand better than a right one because of the thumb).

Each weapon finger is matched to a reference finger (thumb..pinky) by bind-pose
geometry, comparing finger-root directions in the wrist's local frame.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, permutations

from valve_qc_merger.kinematics import world_transforms
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import Node, Smd
from valve_qc_merger.transform import Transform

# Reference (target) rig names, per side. Fingers are thumb..pinky (0..4).
_TARGET_WRIST = {"L": "Bip01_L_Hand", "R": "Bip01_R_Hand"}
_TARGET_WRIST_SOURCE = {"L": "Bone_Lefthand", "R": "Bone_Righthand"}
FINGER_COUNT = 5
MIN_FINGER_JOINTS = 2
MAX_FINGER_JOINTS = 4


@dataclass(frozen=True, slots=True)
class FingerChain:
    """A finger as an ordered chain of bone indices (root..tip).

    Usually three bones, but some rigs use two (e.g. a short thumb), so the
    length is not fixed.
    """

    joints: tuple[int, ...]

    @property
    def root(self) -> int:
        return self.joints[0]

    @property
    def tip(self) -> int:
        return self.joints[-1]


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


def _finger_chain_from(root: int, children: dict[int, list[int]]) -> FingerChain | None:
    """Follow a linear chain from ``root`` to a leaf; return it if finger-shaped.

    A finger is a straight run of bones (each with a single child) ending in a
    leaf, of a plausible length. This rejects the palm, weapon mechanism and
    other branching or single-bone helpers.
    """
    joints = [root]
    current = root
    while len(children.get(current, [])) == 1:
        current = children[current][0]
        joints.append(current)
    if children.get(current):
        return None  # ended on a branch, not a leaf
    if not MIN_FINGER_JOINTS <= len(joints) <= MAX_FINGER_JOINTS:
        return None
    return FingerChain(tuple(joints))


def _find_finger_chains(wrist: int, children: dict[int, list[int]]) -> list[FingerChain]:
    """Finger chains hanging directly off ``wrist`` (root..tip, each a leaf run)."""
    chains: list[FingerChain] = []
    for child in children.get(wrist, []):
        chain = _finger_chain_from(child, children)
        if chain is not None:
            chains.append(chain)
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
) -> tuple[tuple[int, ...], float]:
    """Return the permutation ``p`` (target i -> source p[i]) and its cost.

    Cost is the summed angular mismatch of the paired finger-root directions;
    lower means a better fit.
    """
    best_perm: tuple[int, ...] = tuple(range(len(target_dirs)))
    best_cost = float("inf")
    for perm in permutations(range(len(source_dirs)), len(target_dirs)):
        cost = 0.0
        for target_index, source_index in enumerate(perm):
            a = target_dirs[target_index]
            b = source_dirs[source_index]
            dot = a.x * b.x + a.y * b.y + a.z * b.z
            cost += 1.0 - dot  # 0 when aligned, up to 2 when opposed
        if cost < best_cost:
            best_cost = cost
            best_perm = perm
    return best_perm, best_cost


def _match_finger_pairs(
    source_chains: list[FingerChain],
    source_dirs: list[Vector3],
    target_chains: list[FingerChain],
    target_dirs: list[Vector3],
) -> tuple[tuple[tuple[FingerChain, FingerChain], ...], float]:
    """Pick the ``FINGER_COUNT`` source chains best matching the reference fingers.

    Weapon rigs can expose more than five leaf chains (a two-bone bullet helper
    looks finger-shaped); choosing the geometrically best five rejects those.
    """
    best_pairs: tuple[tuple[FingerChain, FingerChain], ...] = ()
    best_cost = float("inf")
    for subset in combinations(range(len(source_chains)), FINGER_COUNT):
        subset_dirs = [source_dirs[i] for i in subset]
        perm, cost = _best_assignment(subset_dirs, target_dirs)
        if cost < best_cost:
            best_cost = cost
            best_pairs = tuple(
                (source_chains[subset[perm[t]]], target_chains[t])
                for t in range(FINGER_COUNT)
            )
    return best_pairs, best_cost


def _is_hand_bone(bone: int, children: dict[int, list[int]]) -> bool:
    return len(_find_finger_chains(bone, children)) >= FINGER_COUNT


def _wrist_candidates(nodes: list[Node]) -> list[int]:
    """Bones that directly parent at least ``FINGER_COUNT`` finger chains."""
    children = _children_map(nodes)
    return [node.index for node in nodes if _is_hand_bone(node.index, children)]


def _resolve_hand_bone(hint: int, children: dict[int, list[int]]) -> int:
    """From a wrist ``hint``, find the (self or descendant) bone that parents the
    fingers. Some rigs name the wrist but hang the fingers off a palm below it."""
    queue = [hint]
    seen: set[int] = set()
    while queue:
        bone = queue.pop(0)
        if bone in seen:
            continue
        seen.add(bone)
        if _is_hand_bone(bone, children):
            return bone
        queue.extend(children.get(bone, []))
    return hint


def _resolve_finger_pairs(
    source: Smd, target: Smd, side: str, source_wrist: int
) -> tuple[tuple[tuple[FingerChain, FingerChain], ...], float]:
    """Match the weapon's fingers under ``source_wrist`` to the reference fingers."""
    source_chains = _find_finger_chains(source_wrist, _children_map(source.nodes))
    if len(source_chains) < FINGER_COUNT:
        raise CorrespondenceError(
            f"expected at least {FINGER_COUNT} finger chains under the {side} wrist, "
            f"found {len(source_chains)}"
        )
    target_wrist = _index_by_name(target.nodes, _TARGET_WRIST[side])
    if target_wrist is None:
        raise CorrespondenceError(f"reference rig has no {_TARGET_WRIST[side]!r} bone")
    target_chains = _target_finger_chains(target.nodes, side)

    source_world = world_transforms(source.nodes, source.frames[0])
    target_world = world_transforms(target.nodes, target.frames[0])
    source_dirs = _root_directions(source_chains, source_wrist, source_world)
    target_dirs = _root_directions(target_chains, target_wrist, target_world)
    return _match_finger_pairs(source_chains, source_dirs, target_chains, target_dirs)


def _side_cost(source: Smd, target: Smd, side: str, source_wrist: int) -> float:
    """Finger-fan match cost for assigning ``source_wrist`` to ``side``."""
    try:
        return _resolve_finger_pairs(source, target, side, source_wrist)[1]
    except CorrespondenceError:
        return float("inf")


def _resolve_source_wrists(source: Smd, target: Smd) -> dict[str, int]:
    """Return the source wrist bone index for each side ("L"/"R")."""
    children = _children_map(source.nodes)
    named = {
        side: _index_by_name(source.nodes, name)
        for side, name in _TARGET_WRIST_SOURCE.items()
    }
    if all(index is not None for index in named.values()):
        return {
            side: _resolve_hand_bone(index, children)
            for side, index in named.items()
            if index is not None
        }

    candidates = _wrist_candidates(source.nodes)
    if len(candidates) != 2:
        raise CorrespondenceError(
            f"could not identify two wrist bones structurally (found {len(candidates)}); "
            "the rig neither uses Bone_Lefthand/Bone_Righthand nor exposes two "
            "five-finger hands"
        )
    first, second = candidates
    straight = {"L": first, "R": second}
    swapped = {"L": second, "R": first}

    def total(assignment: dict[str, int]) -> float:
        return sum(_side_cost(source, target, side, wrist) for side, wrist in assignment.items())

    return straight if total(straight) <= total(swapped) else swapped


def _build_side(source: Smd, target: Smd, side: str, source_wrist: int) -> HandLink:
    target_wrist = _index_by_name(target.nodes, _TARGET_WRIST[side])
    if target_wrist is None:
        raise CorrespondenceError(f"reference rig has no {_TARGET_WRIST[side]!r} bone")
    finger_pairs, _ = _resolve_finger_pairs(source, target, side, source_wrist)
    return HandLink(side, source_wrist, target_wrist, finger_pairs)


def build_hand_correspondences(source: Smd, target: Smd) -> list[HandLink]:
    """Resolve left- and right-hand correspondence between weapon and reference.

    ``source`` is the weapon reference SMD (whole skeleton, bind pose in frame 0);
    ``target`` is the reference hand SMD. Raises :class:`CorrespondenceError`
    when either rig does not match the expected structure.
    """
    if not source.frames or not target.frames:
        raise CorrespondenceError("both rigs need a bind-pose skeleton frame")
    wrists = _resolve_source_wrists(source, target)
    return [_build_side(source, target, side, wrists[side]) for side in ("L", "R")]


__all__ = [
    "CorrespondenceError",
    "FingerChain",
    "HandLink",
    "build_hand_correspondences",
]
