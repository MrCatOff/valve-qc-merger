"""Hand-size measurement against the reference hands (pure Python, no bpy).

Coverage triage: retarget failures blamed on "hand size" split into two very
different classes. Real size mismatches (source finger chains shorter/longer
than the reference) need the weapon/hand offset compensation; pose and
arm-offset differences need none — and arm bones vary so wildly across rigs
(forearm "lengths" of 0.05x to 2.3x on hands whose fingers match the
reference EXACTLY) that any measure above the wrist is rig noise. Everything
here therefore measures wrist-down only, on rest positions (pose-invariant),
with the thumb excluded — mirroring the worker's ``_auto_hand_offset`` rule.

``grip_measure`` gives rig-agnostic grip scalars (weapon offset along/off the
palm axis) used by the v_deagle/v_g_deagle oracle: those two models share one
weapon mesh (proven identical to 0.01u) but old-style 0.854x hands vs
correctly-proportioned hands, so the gold model records where the weapon must
sit relative to a correct hand — measurable ground truth for the retarget.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from valve_qc_merger.merge_view.hands import match_hands
from valve_qc_merger.merge_view.skeleton_ops import fk_worlds
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import Smd
from valve_qc_merger.retarget.correspondence import RigBone

# Non-thumb fingers, per the auto-offset rule (§7.5): thumb chains diverge
# from the palm axis and would skew the length statistic.
_FINGERS = (1, 2, 3, 4)
_SEGMENTS = ("", "1", "2")  # proximal, middle, distal suffixes


@dataclass(frozen=True)
class HandScale:
    """Wrist-down size of a model's hands relative to the reference hands."""

    ratio: float  # mean source/reference finger-chain length ratio
    surplus: float  # mean (reference - source) chain length, model units
    chains: int  # complete finger chains measured
    renames: dict[str, str]  # source bone -> reference bone


@dataclass(frozen=True)
class GripMeasure:
    """Rig-agnostic weapon-vs-hand scalars (rest/idle pose)."""

    along_palm: float  # weapon offset projected on wrist->knuckle-centroid axis
    off_axis: float  # magnitude of the perpendicular component
    distance: float  # |weapon - wrist|


def _rest_worlds(smd: Smd) -> dict[str, Vector3]:
    worlds = fk_worlds(smd, smd.frames[0])
    name_of = {n.index: n.name for n in smd.nodes}
    return {name_of[i]: t.translation for i, t in worlds.items()}


def _dist(a: Vector3, b: Vector3) -> float:
    return float(((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2) ** 0.5)


def _chain_length(worlds: dict[str, Vector3], wrist: str, bones: list[str]) -> float | None:
    if wrist not in worlds or any(b not in worlds for b in bones):
        return None
    length = _dist(worlds[bones[0]], worlds[wrist])
    for prev, bone in zip(bones, bones[1:], strict=False):
        length += _dist(worlds[bone], worlds[prev])
    return length


def measure_hand_scale(
    mesh: Smd, reference: list[RigBone], reference_smd: Smd,
    include: set[str] | None = None,
) -> HandScale:
    """Match the mesh's hand bones onto the reference and compare chain lengths.

    Raises :class:`~valve_qc_merger.retarget.correspondence.CorrespondenceError`
    when the rig cannot be matched at all (same failure the pipeline reports).
    """
    match = match_hands(mesh, reference, include)
    to_source = {new: old for old, new in match.renames.items()}
    src_worlds = _rest_worlds(mesh)
    ref_worlds = _rest_worlds(reference_smd)

    ratios: list[float] = []
    surpluses: list[float] = []
    chains = 0
    for side in ("L", "R"):
        ref_wrist = f"Bip01 {side} Hand"
        src_wrist = to_source.get(ref_wrist)
        if src_wrist is None:
            continue
        for finger in _FINGERS:
            ref_bones = [f"Bip01 {side} Finger{finger}{seg}" for seg in _SEGMENTS]
            src_bones = [to_source.get(b) for b in ref_bones]
            if any(b is None for b in src_bones):
                continue
            ref_len = _chain_length(ref_worlds, ref_wrist, ref_bones)
            src_len = _chain_length(
                src_worlds, src_wrist, [b for b in src_bones if b is not None]
            )
            if ref_len is None or src_len is None or ref_len <= 0:
                continue
            ratios.append(src_len / ref_len)
            surpluses.append(ref_len - src_len)
            chains += 1
    if not chains:
        return HandScale(ratio=1.0, surplus=0.0, chains=0, renames=dict(match.renames))
    return HandScale(
        ratio=sum(ratios) / len(ratios),
        surplus=sum(surpluses) / len(surpluses),
        chains=chains,
        renames=dict(match.renames),
    )


def dominant_weapon_bone(weapon_mesh: Smd) -> str:
    """The bone carrying the most weapon vertices (the weapon's anchor)."""
    counts = Counter(v.bone for t in weapon_mesh.triangles for v in t.vertices)
    name_of = {n.index: n.name for n in weapon_mesh.nodes}
    return name_of[counts.most_common(1)[0][0]]


def grip_measure(
    smd: Smd, wrist: str, knuckles: list[str], weapon_bone: str
) -> GripMeasure:
    """Weapon position relative to the wrist, decomposed on the palm axis.

    Scalars only — independent of each rig's wrist orientation convention, so
    a decompiled CSO rig and the reference skeleton compare directly.
    """
    worlds = _rest_worlds(smd)
    w = worlds[wrist]
    cen = Vector3(
        sum(worlds[k].x for k in knuckles) / len(knuckles),
        sum(worlds[k].y for k in knuckles) / len(knuckles),
        sum(worlds[k].z for k in knuckles) / len(knuckles),
    )
    axis = Vector3(cen.x - w.x, cen.y - w.y, cen.z - w.z)
    norm = (axis.x**2 + axis.y**2 + axis.z**2) ** 0.5
    axis = Vector3(axis.x / norm, axis.y / norm, axis.z / norm)
    g = worlds[weapon_bone]
    d = Vector3(g.x - w.x, g.y - w.y, g.z - w.z)
    along = d.x * axis.x + d.y * axis.y + d.z * axis.z
    off = Vector3(d.x - along * axis.x, d.y - along * axis.y, d.z - along * axis.z)
    return GripMeasure(
        along_palm=along,
        off_axis=(off.x**2 + off.y**2 + off.z**2) ** 0.5,
        distance=(d.x**2 + d.y**2 + d.z**2) ** 0.5,
    )


# Ground-truth calibration from the v_deagle/v_g_deagle pair, PER SIDE: the
# hand offset per unit of hand-size surplus, in each SOURCE hand's orthonormal
# palm frame, calibrated so each retargeted WRIST lands exactly on the
# authored gold wrist in world space. Sides genuinely differ: CS viewmodels
# are authored left-handed (the game mirrors via cl_righthand), so the LEFT
# hand grips the weapon and the RIGHT supports/reloads — their authored
# offsets are not mirror images.
GUN_SHIFT_PER_SURPLUS = {
    "R": (1.949, -1.336, -0.907),
    "L": (2.668, 0.459, -0.737),
}


def _v(a: Vector3, b: Vector3) -> Vector3:
    return Vector3(b.x - a.x, b.y - a.y, b.z - a.z)


def _dot(u: Vector3, w: Vector3) -> float:
    return u.x * w.x + u.y * w.y + u.z * w.z


def _norm(u: Vector3) -> Vector3:
    m = _dot(u, u) ** 0.5
    return Vector3(u.x / m, u.y / m, u.z / m)


def _cross(u: Vector3, w: Vector3) -> Vector3:
    return Vector3(u.y * w.z - u.z * w.y, u.z * w.x - u.x * w.z,
                   u.x * w.y - u.y * w.x)


def palm_frame(
    worlds: dict[str, Vector3], wrist: str, index: str, pinky: str,
    knuckles: list[str],
) -> tuple[Vector3, Vector3, Vector3]:
    """Orthonormal (palm-forward, across-knuckles, palm-normal) world basis."""
    w = worlds[wrist]
    cen = Vector3(
        sum(worlds[k].x for k in knuckles) / len(knuckles),
        sum(worlds[k].y for k in knuckles) / len(knuckles),
        sum(worlds[k].z for k in knuckles) / len(knuckles),
    )
    a = _norm(_v(w, cen))
    raw = _v(worlds[index], worlds[pinky])
    k = _norm(Vector3(raw.x - _dot(raw, a) * a.x,
                      raw.y - _dot(raw, a) * a.y,
                      raw.z - _dot(raw, a) * a.z))
    return a, k, _cross(a, k)


def auto_hand_offsets(
    scale: HandScale, grip_anim: Smd,
) -> dict[str, Vector3]:
    """Per-side world hand offsets compensating a hand-size mismatch.

    Built on the SOURCE grip pose (frame 0 of an idle-like animation): each
    side's wrist shifts by its calibrated GUN_SHIFT_PER_SURPLUS x surplus in
    that side's palm frame. Sides whose grip bones are absent are omitted.
    """
    to_source = {new: old for old, new in scale.renames.items()}
    worlds = _rest_worlds(grip_anim)
    out: dict[str, Vector3] = {}
    for side in ("R", "L"):
        constants = GUN_SHIFT_PER_SURPLUS[side]
        names = [to_source.get(f"Bip01 {side} Hand"),
                 to_source.get(f"Bip01 {side} Finger1"),
                 to_source.get(f"Bip01 {side} Finger4")]
        knuckles = [to_source.get(f"Bip01 {side} Finger{i}") for i in (1, 2, 3, 4)]
        bones = [*names, *knuckles]
        if any(b is None or b not in worlds for b in bones):
            continue
        a, k, n = palm_frame(worlds, str(names[0]), str(names[1]), str(names[2]),
                             [str(b) for b in knuckles])
        fa, fk, fn = constants
        s = scale.surplus
        out[side] = Vector3(
            -(fa * a.x + fk * k.x + fn * n.x) * s,
            -(fa * a.y + fk * k.y + fn * n.y) * s,
            -(fa * a.z + fk * k.z + fn * n.z) * s,
        )
    return out


GRIP_RIGID_STD = 1.0  # units; wrist-to-gun distance std below this = gripping


def grip_sides(
    scale: HandScale, anims: list[Smd], weapon_bone: str,
) -> list[str]:
    """Sides whose wrist the weapon follows rigidly across the animations.

    CS viewmodels are authored left-handed: the gripping hand is usually the
    LEFT one, and only rigidity tells (the support hand's fingers sit even
    closer to the gun than the gripping hand's). A side counts as gripping
    when its wrist-to-weapon-bone distance stays within GRIP_RIGID_STD across
    every provided animation.
    """
    to_source = {new: old for old, new in scale.renames.items()}
    sides: list[str] = []
    for side in ("R", "L"):
        wrist = to_source.get(f"Bip01 {side} Hand")
        if wrist is None:
            continue
        worst = 0.0
        seen = False
        for anim in anims:
            names = {n.name for n in anim.nodes}
            if wrist not in names or weapon_bone not in names:
                continue
            distances = []
            for frame in anim.frames:
                worlds = fk_worlds(anim, frame)
                idx = {n.name: n.index for n in anim.nodes}
                a = worlds[idx[wrist]].translation
                b = worlds[idx[weapon_bone]].translation
                distances.append(((a.x - b.x) ** 2 + (a.y - b.y) ** 2
                                  + (a.z - b.z) ** 2) ** 0.5)
            if len(distances) < 2:
                continue
            seen = True
            mean = sum(distances) / len(distances)
            std = (sum((d - mean) ** 2 for d in distances) / len(distances)) ** 0.5
            worst = max(worst, std)
        if seen and worst < GRIP_RIGID_STD:
            sides.append(side)
    return sides


def grip_components(
    smd: Smd, wrist: str, index: str, pinky: str, knuckles: list[str],
    weapon_bone: str,
) -> tuple[float, float, float]:
    """Signed weapon-from-wrist components in the orthonormal palm frame."""
    worlds = _rest_worlds(smd)
    a, k, n = palm_frame(worlds, wrist, index, pinky, knuckles)
    d = _v(worlds[wrist], worlds[weapon_bone])
    return _dot(d, a), _dot(d, k), _dot(d, n)


__all__ = [
    "GRIP_RIGID_STD",
    "GUN_SHIFT_PER_SURPLUS",
    "GripMeasure",
    "HandScale",
    "auto_hand_offsets",
    "dominant_weapon_bone",
    "grip_components",
    "grip_measure",
    "grip_sides",
    "measure_hand_scale",
    "palm_frame",
]
