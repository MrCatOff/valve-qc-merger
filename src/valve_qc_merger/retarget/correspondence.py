"""Geometric bone correspondence between two hand rigs (Phase 2a, §7.3).

Pure Python (no ``bpy``): the worker extracts each bone's world rest ``head`` and
``tail`` from Blender into :class:`RigBone` records and calls
:func:`build_correspondence`; the same code is unit-tested outside Blender on
synthetic rigs.

Names never correspond (``Bip01 *`` vs ``BoneNN``), so the map is built from
structure first (wrists have >=4 finger children; each finger is a linear chain)
and geometry second. Each hand gets an intrinsic, chirality-sensitive local frame
built from the palm-forward direction, the knuckle spread and the thumb side; a
left hand and a right hand therefore produce mirrored frames, which is exactly
what lets us pair arms and order fingers without ever trusting a world-axis sign.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.transform import Matrix3


# --------------------------------------------------------------------------- #
# Small vector helpers (Vector3 is a bare NamedTuple)
# --------------------------------------------------------------------------- #
def _sub(a: Vector3, b: Vector3) -> Vector3:
    return Vector3(a.x - b.x, a.y - b.y, a.z - b.z)


def _dot(a: Vector3, b: Vector3) -> float:
    return a.x * b.x + a.y * b.y + a.z * b.z


def _cross(a: Vector3, b: Vector3) -> Vector3:
    return Vector3(a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x)


def _scale(a: Vector3, s: float) -> Vector3:
    return Vector3(a.x * s, a.y * s, a.z * s)


def _norm(a: Vector3) -> Vector3:
    length = a.length()
    return Vector3(a.x / length, a.y / length, a.z / length) if length else a


def _centroid(points: list[Vector3]) -> Vector3:
    n = float(len(points))
    return Vector3(
        sum(p.x for p in points) / n,
        sum(p.y for p in points) / n,
        sum(p.z for p in points) / n,
    )


def _angle(a: Vector3, b: Vector3) -> float:
    d = _dot(_norm(a), _norm(b))
    return math.acos(max(-1.0, min(1.0, d)))


# --------------------------------------------------------------------------- #
# Rig model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RigBone:
    """One bone's world rest pose and parent link."""

    name: str
    parent: str | None
    head: Vector3
    tail: Vector3

    def direction(self) -> Vector3:
        return _sub(self.tail, self.head)


class Rig:
    """A skeleton restricted to a set of bones, with a children index."""

    def __init__(self, bones: list[RigBone], include: set[str] | None = None) -> None:
        keep = {b.name for b in bones} if include is None else include
        self.bones: dict[str, RigBone] = {b.name: b for b in bones if b.name in keep}
        self.children: dict[str, list[str]] = {name: [] for name in self.bones}
        for bone in self.bones.values():
            if bone.parent in self.children:
                self.children[bone.parent].append(bone.name)
        for kids in self.children.values():
            kids.sort()

    def chain_from(self, base: str) -> list[str]:
        """Follow a single-child chain from ``base`` to its tip (inclusive)."""
        chain = [base]
        cursor = base
        while True:
            kids = self.children[cursor]
            if len(kids) != 1:
                break
            cursor = kids[0]
            chain.append(cursor)
        return chain


# --------------------------------------------------------------------------- #
# Structure: arms, wrists, finger chains
# --------------------------------------------------------------------------- #
@dataclass
class Arm:
    """A discovered arm: wrist, proximal forearm chain and five finger chains."""

    wrist: str
    forearm: list[str]  # proximal..wrist ancestors within the rig (root..wrist)
    fingers: list[list[str]]  # five chains, each base..tip
    side: str = "?"  # "L" | "R" | "?" (from the target bone names, if present)
    # Reversed-hierarchy rigs parent the forearm BELOW the hand as a leaf child
    # (grafted CS rigs). When the wrist has no mappable ancestors, this is the
    # forearm bone to map instead.
    reversed_forearm: str | None = None


def _discover_arms(rig: Rig) -> list[Arm]:
    """Wrists = bones with >=4 FINGER chains; each qualifying child seeds one (§7.3).

    A finger chain must have at least two joints. Grafted rigs can parent
    single-bone stubs under the wrist too — a leaf forearm (reversed hierarchies
    put the forearm BELOW the hand), attachment helpers, bullet bones — and
    treating those as fingers poisons the thumb identification: a forearm stub
    is always the planar outlier of the "knuckle" bases.
    """
    arms: list[Arm] = []
    for name, kids in rig.children.items():
        fingers = [
            chain for chain in (rig.chain_from(child) for child in kids)
            if len(chain) >= 2
        ]
        if len(fingers) < 4:
            continue
        forearm = _ancestors(rig, name)
        stubs = [child for child in kids if len(rig.chain_from(child)) < 2]
        arms.append(Arm(
            wrist=name, forearm=forearm, fingers=fingers, side=_side_of(name),
            reversed_forearm=_reversed_forearm(rig, name, fingers, stubs),
        ))
    return arms


def _reversed_forearm(
    rig: Rig, wrist: str, fingers: list[list[str]], stubs: list[str]
) -> str | None:
    """The leaf child that IS the forearm on a reversed-hierarchy rig, if any.

    Distinguished from attachment helpers geometrically: its head sits far from
    the wrist (at the elbow — beyond the knuckle distance) and points opposite
    palm-forward. Helpers sit on the hand itself.
    """
    wrist_head = rig.bones[wrist].head
    bases = [rig.bones[chain[0]].head for chain in fingers]
    palm_forward = _norm(_sub(_centroid(bases), wrist_head))
    knuckle_dist = sum(b.distance_to(wrist_head) for b in bases) / len(bases)
    best: str | None = None
    best_dist = 0.0
    for child in stubs:
        offset = _sub(rig.bones[child].head, wrist_head)
        dist = offset.length()
        if dist <= knuckle_dist:
            continue  # on the hand: an attachment/helper bone
        if _dot(_norm(offset), palm_forward) > -0.5:
            continue  # not pointing back toward the elbow
        if dist > best_dist:
            best, best_dist = child, dist
    return best


def _ancestors(rig: Rig, name: str) -> list[str]:
    chain = [name]
    cursor = rig.bones[name].parent
    while cursor is not None and cursor in rig.bones:
        chain.append(cursor)
        cursor = rig.bones[cursor].parent
    chain.reverse()
    return chain


def _side_of(name: str) -> str:
    tokens = [t.lower() for t in name.replace("_", " ").replace("-", " ").split()]
    if "l" in tokens or any("left" in t for t in tokens):
        return "L"
    if "r" in tokens or any("right" in t for t in tokens):
        return "R"
    return "?"


# --------------------------------------------------------------------------- #
# Geometry: thumb identification and the intrinsic hand frame
# --------------------------------------------------------------------------- #
# Abduction margin (top pick vs runner-up) above which the primary signal is
# considered decisive and may overrule a disagreeing planar cross-check. On a
# curled rest pose (rigs whose reference pose is already mid-grip) the base
# plane is unreliable, while a real thumb's abduction dwarfs every finger's.
_THUMB_DECISIVE_MARGIN = math.radians(15.0)


def _identify_thumb(rig: Rig, arm: Arm, warnings: list[str] | None = None) -> int:
    """Thumb = the chain whose base segment is most abducted from the mean (§7.3.4).

    Cross-checked against the planar-outlier base. The signals disagreeing aborts
    (the spec forbids guessing) — unless abduction, the primary signal, is
    decisive by a clear margin, in which case the fragile cross-check is
    overruled with a warning instead.
    """
    # Base-segment direction = head-to-child-head, never bone.direction(): SMD
    # stores no bone tails, so Blender/BST invents them on import — on the
    # anaconda the fake tails made abduction pick the index as the left thumb
    # (and the knuckle ordering then reversed the whole hand). The >=2-joint
    # chain filter guarantees a real child head exists.
    dirs = [
        _norm(_sub(rig.bones[chain[1]].head, rig.bones[chain[0]].head))
        for chain in arm.fingers
    ]
    abduction: list[float] = []
    for i, di in enumerate(dirs):
        others = [d for j, d in enumerate(dirs) if j != i]
        mean = _norm(_centroid(others))
        abduction.append(_angle(di, mean))
    thumb = max(range(len(dirs)), key=lambda i: abduction[i])

    bases = [rig.bones[chain[0]].head for chain in arm.fingers]
    plane_thumb = _planar_outlier(bases)
    if plane_thumb != thumb:
        ranked = sorted(abduction, reverse=True)
        margin = ranked[0] - ranked[1]
        if margin >= _THUMB_DECISIVE_MARGIN:
            if warnings is not None:
                warnings.append(
                    f"thumb planar cross-check overruled on {arm.wrist}: abduction "
                    f"decisively picks {arm.fingers[thumb][0]} "
                    f"(margin {math.degrees(margin):.0f} deg); planar outlier said "
                    f"{arm.fingers[plane_thumb][0]}"
                )
            return thumb
        raise CorrespondenceError(
            f"thumb signals disagree on {arm.wrist}: abduction picks "
            f"{arm.fingers[thumb][0]} (margin {math.degrees(margin):.0f} deg, "
            f"ambiguous), planar outlier picks {arm.fingers[plane_thumb][0]}"
        )
    return thumb


def _planar_outlier(points: list[Vector3]) -> int:
    """Index of the point whose removal leaves the flattest remaining set."""
    best_i = 0
    best_flatness = math.inf
    for i in range(len(points)):
        rest = [p for j, p in enumerate(points) if j != i]
        centroid = _centroid(rest)
        normal = _best_normal(rest, centroid)
        spread = sum(abs(_dot(_sub(p, centroid), normal)) for p in rest)
        if spread < best_flatness:
            best_flatness = spread
            best_i = i
    return best_i


def _best_normal(points: list[Vector3], centroid: Vector3) -> Vector3:
    """Rough plane normal for near-coplanar points (sum of fan cross products)."""
    normal = Vector3(0.0, 0.0, 0.0)
    n = len(points)
    for i in range(n):
        a = _sub(points[i], centroid)
        b = _sub(points[(i + 1) % n], centroid)
        c = _cross(a, b)
        normal = Vector3(normal.x + c.x, normal.y + c.y, normal.z + c.z)
    return _norm(normal)


@dataclass
class HandFrame:
    """Intrinsic, chirality-sensitive local frame of one hand."""

    origin: Vector3
    u: Vector3  # palm forward (wrist -> non-thumb centroid)
    k: Vector3  # knuckle spread, oriented away from the thumb
    n: Vector3  # palm normal = u x k
    scale: float
    thumb: int
    order: list[int]  # non-thumb chain indices, ascending along k

    def local(self, point: Vector3) -> Vector3:
        d = _sub(point, self.origin)
        return Vector3(_dot(d, self.u) / self.scale,
                       _dot(d, self.k) / self.scale,
                       _dot(d, self.n) / self.scale)


def _hand_frame(rig: Rig, arm: Arm, thumb: int) -> HandFrame:
    wrist_head = rig.bones[arm.wrist].head
    bases = [rig.bones[chain[0]].head for chain in arm.fingers]
    non_thumb = [i for i in range(len(bases)) if i != thumb]

    centroid = _centroid([bases[i] for i in non_thumb])
    u = _norm(_sub(centroid, wrist_head))

    # Knuckle spread: direction between the two farthest non-thumb bases.
    k_raw = _spread_axis([bases[i] for i in non_thumb])
    # Orient k away from the thumb so the frame is consistent across hands.
    if _dot(_sub(bases[thumb], wrist_head), k_raw) > 0.0:
        k_raw = _scale(k_raw, -1.0)
    n = _norm(_cross(u, k_raw))
    k = _norm(_cross(n, u))

    scale = sum(bases[i].distance_to(wrist_head) for i in non_thumb) / len(non_thumb)
    scale = scale or 1.0
    order = sorted(non_thumb, key=lambda i: _dot(_sub(bases[i], wrist_head), k))
    return HandFrame(wrist_head, u, k, n, scale, thumb, order)


def _spread_axis(points: list[Vector3]) -> Vector3:
    best = Vector3(1.0, 0.0, 0.0)
    best_d = -1.0
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            v = _sub(points[i], points[j])
            d = v.length()
            if d > best_d:
                best_d, best = d, v
    return _norm(best)


# --------------------------------------------------------------------------- #
# Correspondence
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BoneMap:
    """One target bone mapped to its source bone (or ``None`` for a held tip)."""

    target: str
    source: str | None
    role: str  # "forearm" | "wrist" | "finger" | "tip"
    side: str


@dataclass
class Correspondence:
    """The full target->source map plus diagnostics (§7.3.7)."""

    maps: list[BoneMap]
    score: float  # orientation-alignment score of the chosen pairing
    margin: float  # score gap to the second-best pairing (confidence)
    warnings: list[str] = field(default_factory=list)
    # Per mapped target bone: (source, target) anatomical rest frames, so Phase 2b
    # can orient the hand to the source's absolute orientation (§7.4 rest divergence).
    frames: dict[str, tuple[Matrix3, Matrix3]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, str | None]:
        return {m.target: m.source for m in self.maps}


class CorrespondenceError(RuntimeError):
    """Rig discovery or correspondence could not be resolved."""


def build_correspondence(
    source_bones: list[RigBone],
    source_hand_names: set[str],
    target_bones: list[RigBone],
    *,
    force_pairing: list[int] | None = None,
) -> Correspondence:
    """Map every target hand bone to its geometric source counterpart (§7.3).

    ``force_pairing`` overrides the automatic arm assignment (target index ->
    source index); used by the ``swap_arms`` config escape hatch.
    """
    src = Rig(source_bones, include=source_hand_names)
    tgt = Rig(target_bones)

    src_arms = _discover_arms(src)
    tgt_arms = _discover_arms(tgt)
    if not src_arms or not tgt_arms:
        raise CorrespondenceError("no wrist (>=4 finger children) found in a rig")
    if len(src_arms) != len(tgt_arms):
        raise CorrespondenceError(
            f"arm count mismatch: source has {len(src_arms)}, target has {len(tgt_arms)}"
        )

    warnings: list[str] = []
    src_frames = [
        _hand_frame(src, arm, _identify_thumb(src, arm, warnings)) for arm in src_arms
    ]
    tgt_frames = [
        _hand_frame(tgt, arm, _identify_thumb(tgt, arm, warnings)) for arm in tgt_arms
    ]

    named = _pairing_by_side_names(src_arms, tgt_arms)
    if named is not None:
        pairing, score, margin = named, math.inf, math.inf
    else:
        pairing, score, margin = _best_pairing(src_frames, tgt_frames)
    if force_pairing is not None:
        if sorted(force_pairing) != list(range(len(tgt_arms))):
            raise CorrespondenceError(f"force_pairing {force_pairing} is not a valid permutation")
        pairing, margin = force_pairing, math.inf
        warnings.append(f"arm pairing forced to {force_pairing}")
    elif margin < 0.5:
        warnings.append(f"ambiguous arm pairing (best-second score gap = {margin:.2f})")

    maps: list[BoneMap] = []
    frames: dict[str, tuple[Matrix3, Matrix3]] = {}
    for tgt_idx, src_idx in enumerate(pairing):
        src_arm, tgt_arm = src_arms[src_idx], tgt_arms[tgt_idx]
        if len(src_arm.fingers) != len(tgt_arm.fingers):
            raise CorrespondenceError(
                f"finger-count mismatch pairing {tgt_arm.wrist} ({len(tgt_arm.fingers)}) "
                f"with {src_arm.wrist} ({len(src_arm.fingers)})"
            )
        arm_maps = _map_arm(
            src, tgt, src_arm, tgt_arm,
            src_frames[src_idx], tgt_frames[tgt_idx],
        )
        maps += arm_maps
        f_src = _frame_matrix(src_frames[src_idx])
        f_tgt = _frame_matrix(tgt_frames[tgt_idx])
        for m in arm_maps:
            if m.source is not None:
                frames[m.target] = (f_src, f_tgt)
    return Correspondence(
        maps=maps, score=score, margin=margin, warnings=warnings, frames=frames
    )


def _frame_matrix(hf: HandFrame) -> Matrix3:
    """The anatomical hand frame as a rotation whose columns are (u, k, n)."""
    return (
        (hf.u.x, hf.k.x, hf.n.x),
        (hf.u.y, hf.k.y, hf.n.y),
        (hf.u.z, hf.k.z, hf.n.z),
    )


def _side_offsets(frames: list[HandFrame]) -> list[Vector3]:
    """Each arm's wrist direction from the arms' shared centroid (its 'side')."""
    centroid = _centroid([f.origin for f in frames])
    return [_norm(_sub(f.origin, centroid)) for f in frames]


def _pairing_by_side_names(src_arms: list[Arm], tgt_arms: list[Arm]) -> list[int] | None:
    """Pair by L/R bone names when BOTH rigs carry unambiguous side labels.

    Rigs like the grafted CS templates name their wrists (``Bip01_L_Hand``,
    ``Bone_Righthand``); names are authoritative where they exist. Geometry
    remains the fallback — some rigs (v_elite's ``BoneNN``) name nothing, and
    the geometric side heuristic fails exactly on rigs whose arms separate
    along depth rather than sideways (the anaconda swapped hands this way).
    """
    src_sides = [arm.side for arm in src_arms]
    tgt_sides = [arm.side for arm in tgt_arms]
    if "?" in src_sides or "?" in tgt_sides:
        return None
    if len(set(src_sides)) != len(src_sides) or sorted(src_sides) != sorted(tgt_sides):
        return None
    return [src_sides.index(side) for side in tgt_sides]


def _best_pairing(
    src_frames: list[HandFrame], tgt_frames: list[HandFrame],
) -> tuple[list[int], float, float]:
    """Choose the source-arm assignment by matching each arm's side (§7.3).

    A flat T-pose reference hand has no intrinsic chirality (a flat left and right
    hand are exact mirrors), and its bind orientation shares nothing with the
    gripping source pose, so orientation- and chirality-based pairing are both
    unreliable here. What *is* stable is spatial arrangement: both rigs are authored
    in the same world, so the left arm sits to one side and the right to the other.
    Pair arms whose wrist directions from the arm centroid best agree. This needs
    the arms to be spatially separated; if they are not, the score gap is small and
    a warning fires (use the ``swap_arms`` override).

    Returns the assignment (target index -> source index), its score, and the gap
    to the second-best assignment (the pairing confidence).
    """
    from itertools import permutations

    src_off = _side_offsets(src_frames)
    tgt_off = _side_offsets(tgt_frames)
    scored: list[tuple[float, tuple[int, ...]]] = []
    for perm in permutations(range(len(src_frames))):
        total = sum(
            _dot(src_off[src_idx], tgt_off[tgt_idx])
            for tgt_idx, src_idx in enumerate(perm)
        )
        scored.append((total, perm))
    scored.sort(reverse=True)
    best, best_perm = scored[0]
    second = scored[1][0] if len(scored) > 1 else best - 1e9
    return list(best_perm), best, best - second


def _map_arm(
    src: Rig, tgt: Rig, src_arm: Arm, tgt_arm: Arm,
    src_frame: HandFrame, tgt_frame: HandFrame,
) -> list[BoneMap]:
    """Map wrist, forearm and every finger joint for one paired arm."""
    side = tgt_arm.side
    maps: list[BoneMap] = [BoneMap(tgt_arm.wrist, src_arm.wrist, "wrist", side)]

    # Forearm: align distal-to-proximal so the wrist-adjacent bones pair up. Skip
    # a shared root (parent is None): in a Bip01 rig both arms hang off the single
    # Bip01 root, which cannot follow two different source bones, so it is held.
    for tgt_bone, src_bone in zip(
        reversed(tgt_arm.forearm[:-1]), reversed(src_arm.forearm[:-1]), strict=False
    ):
        if tgt.bones[tgt_bone].parent is None or src.bones[src_bone].parent is None:
            continue
        maps.append(BoneMap(tgt_bone, src_bone, "forearm", side))

    # Reversed-hierarchy source (forearm is a leaf child of the hand): the wrist
    # has no mappable ancestors, so map the reference forearm to that leaf — an
    # unmapped reference forearm would hold its T-pose rest orientation and jut
    # out sideways while the hand tracks the weapon.
    if (not any(m.role == "forearm" for m in maps)
            and src_arm.reversed_forearm is not None):
        for tgt_bone in reversed(tgt_arm.forearm[:-1]):
            if tgt.bones[tgt_bone].parent is None:
                continue
            maps.append(BoneMap(tgt_bone, src_arm.reversed_forearm, "forearm", side))
            break

    # Fingers: thumb->thumb, the rest by knuckle order.
    tgt_by_slot = [tgt_frame.thumb, *tgt_frame.order]
    src_by_slot = [src_frame.thumb, *src_frame.order]
    for tgt_i, src_i in zip(tgt_by_slot, src_by_slot, strict=True):
        tgt_chain = tgt_arm.fingers[tgt_i]
        src_chain = src_arm.fingers[src_i]
        for depth, tgt_bone in enumerate(tgt_chain):
            src_finger = src_chain[depth] if depth < len(src_chain) else None
            role = "tip" if src_finger is None else "finger"
            maps.append(BoneMap(tgt_bone, src_finger, role, side))
    return maps


__all__ = [
    "RigBone",
    "Rig",
    "Arm",
    "BoneMap",
    "Correspondence",
    "CorrespondenceError",
    "build_correspondence",
]
