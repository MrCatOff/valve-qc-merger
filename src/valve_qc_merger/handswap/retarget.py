"""Retarget the original hand animation onto the CSO rig — closed form.

Per side, at the setup state (frame 0 of the first sequence — the real
in-game grip, unlike the SMD bind pose which may never occur on screen):

  * the original palm frame (from joint positions) and the CSO palm frame
    give one rigid transform `desired` that lands the CSO palm on the
    original palm — the thumb choice, an optional mirror flip and the
    finger pairing are searched jointly by geometry (predicted chain
    clouds), because mispairing is exactly what twists fingers;
  * c_hand = M_oldwrist(setup)^-1 @ desired is a CONSTANT offset: at any
    frame the CSO hand world is M_oldwrist(t) @ c_hand (the palm follows
    the original wrist rigidly, translation included);
  * each paired finger bone gets a constant offset K aligning the two
    bones' anatomical frames: desired_world_rot(t) = old_world_rot(t) @ K.

The arm is constructed directly (no iterative IK): the elbow sits on the
original forearm line at the CSO forearm length from the wrist, the
shoulder continues along that line off-screen, and the forearm assembly's
free roll about the elbow-wrist chord takes the twist component of the
desired hand rotation (this is what keeps wrists from twisting).

Hand-bone keys are rotation-only: every local translation is pinned to
the rig's rest value — the same shape as Valve's own viewmodel data.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np

from .anatomy import bone_anat_3x3, hand_frame
from .surface import points_to_mesh
from .asset import HandsAsset, SideRig
from .identify import HandInfo, OrigSkeleton
from .math3d import (axis_angle, compose, inv_rigid, normalized,
                     rotation_between, twist_angle)

FALLBACK_ARM_DIR = np.array([0.0, 8.0, -6.0])  # behind and below the view
# bumped when the retarget's result changes for the same input: swap-hands
# assets made by an older revision are re-made by builds (status: stale).
# 2: hand sides by their own shape, worn weapons left alone, shoulders off screen
# 3: weapon geometry on the old arms kept on carrier bones; QC hands dropped at 50 %
# 4: arm parts turn frame to frame no faster than the original's (no elbow/shoulder jumps)
# 5: hand-named textures (Valve's view_skin) cut with the hands; rigid hand meshes kept
# 6: exact texel UVs (the decompiler slid UV islands up to a texel: dark seams)
# 7: an arm the original keeps out of view stays out (v_c4's draw)
# 8: fingers fitted to BOTH hands' skin (a model with rhand + lhand meshes fitted its left by tips)
# 9: a five-finger hand's thumb is the root off the knuckle line (v_ddeagle's right hand took its
#    pinky for the thumb: every finger of that hand crossed); unnamed hands are sided by position
#    (the left one farther along +x), not by palm chirality (v_ddeagle's hands were swapped)
# 10: the same, for projects already redone at 9 during development
RETARGET_REVISION = 10

# Wrist kink of OUR hand mesh as CSO's own animators pose it: the forearm
# direction in the palm frame, relative to the rest pose, (pitch, yaw) in
# degrees — median and p2..p98, measured on the native CSO 2009 weapons. A
# foreign animation's wrist bend was made for ITS rig's hinge; copied
# verbatim onto our mesh it overbends ("---^--" at the wrist). Each frame
# the bend is pulled toward the native median (WRIST_KEEP of the excess
# survives) and clamped to the native range; the hand and grip stay put, the
# forearm (elbow) moves.
WRIST_KEEP = 0.5
NATURAL_WRIST = True
# keep the shoulder off screen (see _hidden_shoulder)
HIDE_SHOULDERS = True
WRIST_NATIVE = {  # side: ((pitch median, lo, hi), (yaw median, lo, hi))
    "left": ((-1.1, -32.3, 21.9), (-0.4, -28.6, 26.3)),
    "right": ((12.3, -46.5, 38.2), (-6.3, -29.9, 51.1)),
}


def _palm_angles(palm: np.ndarray, back: np.ndarray) -> tuple[float, float]:
    """(pitch, yaw) of the wrist->elbow direction in a palm frame (X fingers,
    Y across toward the thumb, Z normal); a straight arm is (0, 0)."""
    x, y, z = palm[:3, 0], palm[:3, 1], palm[:3, 2]
    ahead = -float(back @ x)
    return (float(np.degrees(np.arctan2(back @ z, ahead))),
            float(np.degrees(np.arctan2(back @ y, ahead))))


def _natural_arm(palm: np.ndarray, back: np.ndarray, rest: tuple[float, float],
                 side: str) -> np.ndarray:
    """``back`` (unit wrist->elbow) with its kink pulled into the native range."""
    native = WRIST_NATIVE.get(side)
    if native is None:
        return back
    pitch, yaw = _palm_angles(palm, back)
    new = []
    for value, rest_value, (median, lo, hi) in zip((pitch, yaw), rest, native, strict=True):
        rel = value - rest_value
        rel = median + WRIST_KEEP * (rel - median)
        new.append(rest_value + min(max(rel, lo), hi))
    x, y, z = palm[:3, 0], palm[:3, 1], palm[:3, 2]
    out = -x + np.tan(np.radians(new[1])) * y + np.tan(np.radians(new[0])) * z
    return out / np.linalg.norm(out)


@dataclass
class SidePlan:
    side: str
    hand: HandInfo
    rig: SideRig
    desired_setup: np.ndarray                  # hand world at setup
    c_hand: np.ndarray                         # oldwrist^-1 @ desired
    pairs: list[tuple[list[str], list[str]]]   # (old chain, cso chain)
    finger_k: list[tuple[str, str, np.ndarray]]
    elbow_ref: str | None                      # original forearm bone
    fit_cost: float = 0.0
    _prev_dir: np.ndarray | None = None
    # frame-to-frame continuity within a sequence (reset_motion starts one)
    _prev_orig: np.ndarray | None = None  # the original forearm direction
    _prev_d: np.ndarray | None = None  # our forearm direction (wrist -> elbow)
    _prev_u: np.ndarray | None = None  # our upper arm direction (elbow -> shoulder)


@dataclass
class RetargetPlan:
    asset: HandsAsset
    sides: list[SidePlan] = field(default_factory=list)

    def cso_bones(self) -> list[str]:
        """Ordered CSO bones actually driven (sides present in the model)."""
        out = []
        for sp in self.sides:
            r = sp.rig
            out.extend(r.arm)
            out.append(r.wrist)
            for ch in r.chains:
                out.extend(ch)
            out.extend(r.extras)
        return out


def _chain_cloud_cso(asset: HandsAsset, rig: SideRig,
                     chain: list[str]) -> list[np.ndarray]:
    inv_hand = inv_rigid(asset.bones[rig.wrist].rest_world)
    pts = [asset.head(b) for b in chain] + [asset.tail(chain[-1])]
    return [inv_hand[:3, :3] @ p + inv_hand[:3, 3] for p in pts]


def _chain_cloud_old(skel: OrigSkeleton, world, hand: HandInfo,
                     chain: list[str]) -> list[np.ndarray]:
    pts = [world[b][:3, 3] for b in chain]
    leaf = chain[-1]
    if leaf in hand.tip_dirs:
        d_bind = hand.tip_dirs[leaf]
        rot = world[leaf][:3, :3] @ skel.bind_world[leaf][:3, :3].T
        seg = np.linalg.norm(pts[-1] - pts[-2]) if len(pts) > 1 else 1.0
        pts.append(pts[-1] + (rot @ d_bind) * seg)
    return pts


def knuckle_thumb(roots: list[np.ndarray]) -> int:
    """The finger whose root lies off the line of the others' (the
    knuckles): the thumb, by anatomy."""
    def off_line(t: int) -> float:
        others = np.array([r for i, r in enumerate(roots) if i != t])
        return float(np.linalg.svd(others - others.mean(axis=0), compute_uv=False)[1:].sum())
    return min(range(len(roots)), key=off_line)


def build_side_plan(asset: HandsAsset, skel: OrigSkeleton, hand: HandInfo,
                    setup_world: dict[str, np.ndarray], log=print,
                    grip_offset=None, weapon_offset=None) -> SidePlan:
    """grip_offset: optional (dx, dy, dz) in PALM-FRAME axes (X fingers-
    forward, Y across the palm toward the thumb, Z the palm normal) that
    shifts where the CSO palm lands relative to the original palm —
    the per-weapon tuning knob for how the weapon sits in the hand.

    weapon_offset: optional (x, y, z) in MODEL space at the setup frame:
    moves the weapon by that vector relative to this hand (the hand is
    shifted by the opposite vector; the offset then rides the wrist like
    every other part of the grip)."""
    rig = asset.sides[hand.side]
    l_cso = rig.palm_local
    # the finger pairing is searched on the PLAIN seat; a grip offset only
    # moves the chosen seat afterwards (applied before the search, an old
    # tuning offset on v_deagle picked the wrong thumb and crossed every
    # finger of that hand)
    l_cso_inv = inv_rigid(l_cso)

    cso_clouds = [_chain_cloud_cso(asset, rig, ch) for ch in rig.chains]
    old_clouds = [_chain_cloud_old(skel, setup_world, hand, ch)
                  for ch in hand.chains]
    w = setup_world[hand.wrist][:3, 3]
    old_roots = [setup_world[ch[0]][:3, 3] for ch in hand.chains]
    hand_rest = asset.bones[rig.wrist].rest_world

    # The CSO thumb is chains[0] (FINGERS ordering). The search is
    # anatomically constrained: the candidate thumb ALWAYS pairs to the
    # CSO thumb, and the remaining fingers map in order along the knuckle
    # line — fingers cannot cross. A free permutation search can win on
    # cloud cost with an impossible assignment (f2000: thumb paired to
    # the index, folding the CSO thumb invisibly along it).
    # With five fingers the thumb is fixed by anatomy, not by the cloud fit:
    # a poor fit (a low-poly hand, ~1.7 u) can score the pinky as the thumb
    # under a flipped frame (v_ddeagle, v_elite, v_sg550's right hand; the
    # two agree on 665 of 668 corpus hands).
    n = min(len(hand.chains), len(rig.chains))
    cso_thumb = 0
    best = None
    thumbs = [knuckle_thumb(old_roots)] if len(hand.chains) >= 5 else range(len(hand.chains))
    for thumb_i in thumbs:
        anchor_pts = old_clouds[thumb_i]
        anchor = anchor_pts[-1]
        for flip in (False, True):
            a_old, _ = hand_frame(w, old_roots, thumb_i=thumb_i,
                                  thumb_anchor=anchor, flip=flip)
            desired = a_old @ l_cso_inv           # -> hand world
            # predicted world position of each CSO chain under this grip
            t_pred = desired @ inv_rigid(hand_rest)
            pred = [[t_pred[:3, :3] @ (hand_rest[:3, :3] @ p
                                       + hand_rest[:3, 3]) + t_pred[:3, 3]
                     for p in cl] for cl in cso_clouds]
            cost = np.zeros((len(old_clouds), len(pred)))
            for i, oc in enumerate(old_clouds):
                for j, pc in enumerate(pred):
                    m = min(len(oc), len(pc))
                    cost[i, j] = np.mean([np.linalg.norm(oc[k] - pc[k])
                                          for k in range(m)])
            y_axis = a_old[:3, 1]
            old_rest = sorted(
                (i for i in range(len(hand.chains)) if i != thumb_i),
                key=lambda i: float(np.dot(old_roots[i], y_axis)))
            cso_rest = sorted(
                (j for j in range(len(rig.chains)) if j != cso_thumb),
                key=lambda j: float(np.dot(pred[j][0], y_axis)))
            k = min(len(old_rest), len(cso_rest))
            for pick in itertools.combinations(range(len(cso_rest)),
                                               k):
                assign = [(thumb_i, cso_thumb)] + \
                    [(old_rest[a], cso_rest[pick[a]]) for a in range(k)]
                total = sum(cost[i, j] for i, j in assign)
                if best is None or total < best[0]:
                    best = (total, thumb_i, assign, desired, a_old, flip)

    total, thumb_i, assign, desired, a_old, _flip = best
    if grip_offset is not None:
        shift = np.eye(4)
        shift[:3, 3] = grip_offset
        desired = a_old @ shift @ l_cso_inv
    if weapon_offset is not None:
        desired = desired.copy()
        desired[:3, 3] -= np.asarray(weapon_offset, dtype=float)
    pairs = [(hand.chains[i], rig.chains[j]) for i, j in assign]
    log("  %s: thumb %r (fit %.2f%s), pairing %s"
        % (hand.side, hand.chains[thumb_i][0], total / max(n, 1),
           ", palm flipped" if _flip else "",
           ", ".join("%s->%s" % (o[0], c[0]) for o, c in pairs)))

    old_across = a_old[:3, 1]
    cso_across = rig.palm_world[:3, 1]

    finger_k = []
    for old_chain, cso_chain in pairs:
        n_pair = min(len(old_chain), len(cso_chain))
        prev_y_o = None
        for i in range(n_pair):
            o, c = old_chain[i], cso_chain[i]
            r_o = setup_world[o][:3, :3]
            r_c = asset.bones[c].rest_world[:3, :3]
            if i + 1 < len(old_chain):
                y_o = (setup_world[old_chain[i + 1]][:3, 3]
                       - setup_world[o][:3, 3])
            elif o in hand.tip_dirs:
                y_o = (r_o @ skel.bind_world[o][:3, :3].T
                       @ hand.tip_dirs[o])
            else:
                y_o = prev_y_o if prev_y_o is not None else \
                    setup_world[o][:3, 3] - setup_world[old_chain[0]][:3, 3]
            if i + 1 < len(cso_chain):
                y_c = asset.head(cso_chain[i + 1]) - asset.head(c)
            else:
                y_c = asset.tail(c) - asset.head(c)
            prev_y_o = np.array(y_o, dtype=float)
            a_o = bone_anat_3x3(y_o, old_across)
            a_c = bone_anat_3x3(y_c, cso_across)
            k = r_o.T @ a_o @ a_c.T @ r_c
            finger_k.append((o, c, k))

    c_hand = inv_rigid(setup_world[hand.wrist]) @ desired
    elbow_ref = skel.parent.get(hand.wrist)
    return SidePlan(side=hand.side, hand=hand, rig=rig,
                    desired_setup=desired, c_hand=c_hand, pairs=pairs,
                    finger_k=finger_k, elbow_ref=elbow_ref,
                    fit_cost=total / max(n, 1))


def build_plan(asset: HandsAsset, skel: OrigSkeleton, hands: list[HandInfo],
               setup_world: dict[str, np.ndarray], log=print,
               grip_offsets: dict | None = None,
               weapon_offset=None) -> RetargetPlan:
    plan = RetargetPlan(asset=asset)
    for hand in hands:
        if hand.side not in asset.sides:
            log("  WARNING: no CSO rig for side %r — skipped" % hand.side)
            continue
        off = (grip_offsets or {}).get(hand.side)
        plan.sides.append(build_side_plan(asset, skel, hand, setup_world,
                                          log, grip_offset=off,
                                          weapon_offset=weapon_offset))
    return plan


_TRIANGLE_CACHE: dict[int, tuple[np.ndarray, np.ndarray]] = {}


def _hand_triangles(asset: HandsAsset) -> tuple[np.ndarray, np.ndarray]:
    """The asset's triangles as (T,3) dominant bone names and (T,3,3) corner
    positions in each corner's own bone rest frame."""
    cached = _TRIANGLE_CACHE.get(id(asset))
    if cached is None:
        inverse = {name: inv_rigid(bone.rest_world) for name, bone in asset.bones.items()}
        names, local = [], []
        for tri in asset.triangles:
            row_names, row_local = [], []
            for corner in tri["corners"]:
                dom = max(corner["weights"], key=lambda bw: bw[1])[0]
                inv = inverse[dom]
                row_names.append(dom)
                row_local.append(inv[:3, :3] @ np.asarray(corner["pos"], dtype=float)
                                 + inv[:3, 3])
            names.append(row_names)
            local.append(row_local)
        cached = (np.array(names), np.array(local, dtype=float))
        _TRIANGLE_CACHE[id(asset)] = cached
    return cached


def seat_offsets(plan: RetargetPlan, setup_world_old: dict[str, np.ndarray], weapon,
                 allowed: dict[str, float] | None = None,
                 log=print) -> dict[str, list[float]]:
    """Per side, the smallest palm shift (palm axes, like ``grip_offset``)
    that takes the PALM's skin out of the weapon.

    The palm is seated joint on joint: our thicker palm then lies deeper in
    whatever the original palm lay against (v_c4: 0.06 u -> 0.27 u on 95 % of
    the frames). Only the excess over ``allowed[side]`` (the original palm's
    own depth, at least PEN_TOLERANCE) counts, the worst tenth of the palm's
    skin decides, and a shift must buy SEAT_SHIFT_COST of it per unit — so a
    palm that is no deeper than the original's stays where it is.
    Rigid translation only (a free rigid fit of the palm was measured worse
    on the corpus); the fingers are fitted afterwards."""
    asset = plan.asset
    out: dict[str, list[float]] = {}
    steps = np.arange(-SEAT_MAX_SHIFT, SEAT_MAX_SHIFT + 1e-9, SEAT_STEP)
    reset_motion(plan)
    posed = solve_frame(plan, setup_world_old)
    tri_bones, tri_local = _hand_triangles(asset)
    for sp in plan.sides:
        rig = sp.rig
        # every triangle with a corner on the palm, as drawn at the setup
        # grip: the webs to the thumb's and the fingers' bases too
        rows = np.isin(tri_bones, [rig.wrist, *rig.extras]).any(axis=1)
        if not rows.any():
            continue
        corners = np.empty_like(tri_local[rows])
        for name in np.unique(tri_bones[rows]):
            m = posed.get(name, asset.bones[name].rest_world)
            sel = tri_bones[rows] == name
            corners[sel] = tri_local[rows][sel] @ m[:3, :3].T + m[:3, 3]
        a, b, c = corners[:, 0], corners[:, 1], corners[:, 2]
        pts = np.unique(np.round(np.concatenate(
            [a, b, c, (a + b) / 2, (b + c) / 2, (c + a) / 2, (a + b + c) / 3]), 5), axis=0)
        axes = (sp.desired_setup @ rig.palm_local)[:3, :3]
        limit = max(PEN_TOLERANCE, (allowed or {}).get(sp.side, 0.0))
        margin = SEAT_MAX_SHIFT + 2.5
        shell = weapon.within(pts.min(axis=0) - margin, pts.max(axis=0) + margin)
        if not len(shell):
            continue
        tenth = max(1, int(len(pts) * SEAT_WORST_FRACTION))

        def excess(offset, pts=pts, axes=axes, shell=shell, limit=limit, tenth=tenth):
            depth = shell.depth(pts + axes @ np.asarray(offset, dtype=float))
            return float(np.sort(np.clip(depth - limit, 0.0, None))[-tenth:].mean())

        start = excess((0.0, 0.0, 0.0))
        if start <= 0.0:
            continue
        best = (start, (0.0, 0.0, 0.0), start)
        for dx in steps:
            for dy in steps:
                for dz in steps:
                    shift = float(np.sqrt(dx * dx + dy * dy + dz * dz))
                    if shift > SEAT_MAX_SHIFT + 1e-9 or SEAT_SHIFT_COST * shift >= best[0]:
                        continue
                    left = excess((dx, dy, dz))
                    cost = left + SEAT_SHIFT_COST * shift
                    if cost < best[0] - 1e-6:
                        best = (cost, (float(dx), float(dy), float(dz)), left)
        if any(best[1]):
            out[sp.side] = [round(v, 3) for v in best[1]]
            log("  seat %s palm %s (palm axes): excess depth %.2f -> %.2f"
                % (sp.side, out[sp.side], start, best[2]))
    return out


CONTACT_RANGE = 0.35  # a part of the original hand this near the weapon holds it
CONTACT_STEP = 0.05
CONTACT_SHIFT_COST = 0.02  # per squared unit of palm shift


def contact_seat(plan: RetargetPlan, setup_world_old: dict[str, np.ndarray], weapon,
                 old_skin: dict[str, np.ndarray], log=print) -> dict[str, list[float]]:
    """Per side, the palm shift (palm axes, like ``grip_offset``) that gives
    our hand the ORIGINAL hand's contact with the weapon.

    Seated joint on joint, our hand holds the weapon differently from the
    original although its joints are where the original's were: our palm is
    thicker toward the weapon and our fingers are thinner, so the grip sits
    deeper in the palm while the finger pads stand off it (v_elite's left:
    palm side +0.01 -> -0.12 u, pads -0.07 -> +0.05 u) — "glued to the palm,
    fingers not touching". Both errors have one cure: move the palm.

    For every part of the original hand that holds the weapon (its skin
    within CONTACT_RANGE of it) the skin-to-weapon distance is the target —
    positive a gap, negative that deep inside; the shift that brings our
    parts' distances nearest to them, least squares, is taken."""
    from .contact import signed_distance, surface_samples
    asset = plan.asset
    reset_motion(plan)
    posed = solve_frame(plan, setup_world_old)
    tri_bones, tri_local = _hand_triangles(asset)
    lead = tri_bones[:, 0]
    out: dict[str, list[float]] = {}
    for sp in plan.sides:
        rig = sp.rig
        part_of = dict.fromkeys([rig.wrist, *rig.extras], "palm")
        old_of = {sp.hand.wrist: "palm", sp.hand.fan: "palm"}
        for old_chain, cso_chain in sp.pairs:
            for o, c in zip(old_chain, cso_chain, strict=False):
                part_of[c] = c
                old_of[o] = c
        target: dict[str, float] = {}
        for bone, part in old_of.items():
            if bone in old_skin:
                dist = float(signed_distance(weapon, surface_samples(old_skin[bone])).min())
                target[part] = min(target.get(part, dist), dist)
        target = {p: d for p, d in target.items() if d < CONTACT_RANGE}
        ours: dict[str, np.ndarray] = {}
        for part in target:
            rows = np.array([part_of.get(b) == part for b in lead])
            if not rows.any():
                continue
            corners = np.empty_like(tri_local[rows])
            for name in np.unique(tri_bones[rows]):
                m = posed.get(name, asset.bones[name].rest_world)
                sel = tri_bones[rows] == name
                corners[sel] = tri_local[rows][sel] @ m[:3, :3].T + m[:3, 3]
            ours[part] = surface_samples(corners)
        if len(ours) < 3:
            continue
        axes = (sp.desired_setup @ rig.palm_local)[:3, :3]
        cloud = np.concatenate(list(ours.values()))
        shell = weapon.within(cloud.min(axis=0) - 4.0, cloud.max(axis=0) + 4.0)

        def miss(offset, ours=ours, target=target, axes=axes, shell=shell):
            move = axes @ np.asarray(offset, dtype=float)
            errors = [float(signed_distance(shell, pts + move).min()) - target[part]
                      for part, pts in ours.items()]
            return float(np.mean(np.square(errors)))

        def search(centre, span, step):
            best = (miss(centre) + CONTACT_SHIFT_COST * float(np.dot(centre, centre)), centre)
            grid = np.arange(-span, span + 1e-9, step)
            for dx in grid:
                for dy in grid:
                    for dz in grid:
                        cand = (centre[0] + dx, centre[1] + dy, centre[2] + dz)
                        size = float(np.dot(cand, cand))
                        if size > SEAT_MAX_SHIFT ** 2 + 1e-9:
                            continue
                        cost = miss(cand) + CONTACT_SHIFT_COST * size
                        if cost < best[0] - 1e-9:
                            best = (cost, cand)
            return best[1]

        start = miss((0.0, 0.0, 0.0))
        coarse = search((0.0, 0.0, 0.0), 0.6, 0.2)
        fine = search(coarse, 0.1, CONTACT_STEP)
        if any(abs(v) > 1e-9 for v in fine):
            out[sp.side] = [round(float(v), 3) for v in fine]
            log("  contact seat %s palm %s (palm axes): contact miss %.2f -> %.2f u rms"
                " over %d parts"
                % (sp.side, out[sp.side], start ** 0.5, miss(fine) ** 0.5, len(ours)))
    return out


MANUAL_CURL_RANGE = np.radians(45.0)  # what --curl may add beyond the auto clamp
SURFACE_MIN_ERR = 0.02  # a finger already this close to the original's skin is left alone
ABDUCTION_DEG = 10.0  # sideways swing allowed per joint (surface fit only)
# --- the weapon as a collider (weapon-aware fit) ---------------------------
# our skin may sit this deep in the weapon without it counting: authored CSO
# grips sink about this much themselves
PEN_TOLERANCE = 0.1
# ... or as deep as the ORIGINAL hand's same part sat (the low-poly Valve
# hands cut into the weapon by design here and there: v_awp's palm is 0.42 u
# inside the stock on every frame) plus this slack
PEN_SLACK = 0.02
PEN_WEIGHT = 10.0  # a unit of mean excess depth against a unit of skin distance
THUMB_ABDUCTION_DEG = 25.0  # the thumb opposes: wider than the fingers' splay
# --- coupled curl ------------------------------------------------------------
# Authored grips of these hands bend the MIDDLE joint hard and the last one
# little (v_ak74u, v_m1911a1: knuckle 44, middle 93, last 39 deg). Fitted
# joint by joint to a small hand's skin, ours came out the other way round
# (41 / 51 / 90): the last joint curls to its clamp and digs the tip in.
# Coupled, the last joint is not a freedom: it follows the middle one.
DIP_RATIO = 0.42  # last joint's bend / middle joint's bend
COUPLED_MID_RANGE = np.radians(60.0)  # what the middle joint may add to reach it
SLAVE_RANGE = np.radians(90.0)
SEAT_MAX_SHIFT = 0.6  # the palm seat moves at most this far (units)
SEAT_STEP = 0.1
SEAT_SHIFT_COST = 0.15  # excess depth a unit of palm shift must buy
SEAT_WORST_FRACTION = 0.03  # of the palm's skin samples: their mean excess decides



def _search(err, current: float, lim: float, step: float) -> float:
    """1-D search of ``err(t)`` over [-lim, lim]: a coarse grid (3 steps),
    then the fine step around the coarse best. Prefers the smaller |t| on
    near-ties (no gratuitous bending)."""
    def scan(values, best):
        for t in values:
            e = err(t)
            if e < best[0] - 1e-4 or (e < best[0] + 1e-4 and abs(t) < abs(best[1])):
                best = (e, t)
        return best
    best = (err(current), current)
    coarse = step * 3
    best = scan(np.arange(-lim, lim + 1e-9, coarse), best)
    centre = best[1]
    fine = np.arange(max(-lim, centre - coarse), min(lim, centre + coarse) + 1e-9, step)
    return float(scan(fine, best)[1])


def refine_finger_fit(plan: RetargetPlan, skel: OrigSkeleton,
                      setup_world_old: dict[str, np.ndarray],
                      *, max_deg: float = 18.0, min_err: float = 0.35,
                      manual=None, old_centroids=None, old_skin=None,
                      weapon=None, allowed=None, coupled=False, log=print):
    """Snug the fingers against the weapon (polish pass, run once).

    The CSO fingers are longer/chunkier than the originals, so driving
    them with the original joint rotations makes the tips overshoot and
    stick out past whatever the original hand was wrapped around. The
    original FINGERTIP positions are snug by construction (the animators
    posed them against the weapon), so each CSO finger gets extra curl —
    about its anatomical flexion axis only, per-joint clamped — chosen by
    coordinate descent to bring its skeletal tip onto the original tip
    position at the setup grip. The result is baked into the constant K
    offsets and rides the whole animation.

    Constrained by design (lesson from the old pipeline: unconstrained
    surface fitting folds hands into garbage): curl is the only DOF, hard
    per-joint clamp, fingers already within min_err are left alone.

    old_centroids: bone -> world skin centroid of the ORIGINAL hands at the
    setup grip (build.skin_centroids). When given, the fit compares like with
    like — the centroid of the distal segment's skin on both hands — instead
    of two rigs' different notions of a "fingertip" (a Blender tail vs. an
    SMD direction estimate), which bent fingers even between identical hands.

    old_skin: bone -> (T,3,3) original skin triangles at the setup grip
    (build.skin_triangles). When given, each finger is fitted to the
    ORIGINAL finger's surface — the mean distance from our finger's skin to
    it — rather than through one tip point: the whole finger lies where the
    original did. Same single DOF (flexion) and the same per-joint clamp.

    weapon: a contact.Shell of the kept weapon geometry at the setup grip.
    When given, the fit also keeps our skin OUT of the weapon: each finger
    bone's skin may sit no deeper than ``allowed[cso bone]`` (how deep the
    original hand's paired bone sat; at least PEN_TOLERANCE), and the excess
    is weighed against the distance to the original skin. A finger that is
    close to the original's but inside the weapon is no longer left alone,
    and the thumb gets its wider sideways swing (it opposes).

    manual: {(side, finger_prefix): extra_degrees} applied per joint on
    top of the automatic result: positive closes the finger, negative opens
    it, on either hand (clamped to max_deg + 45 deg)."""
    asset = plan.asset
    step = np.radians(2.0)
    max_auto = np.radians(max_deg)
    reset_motion(plan)
    base_cso = solve_frame(plan, setup_world_old)

    for sp in plan.sides:
        rig = sp.rig
        hand_w = base_cso[rig.wrist]
        new_k: dict[str, np.ndarray] = {}

        for old_chain, cso_chain in sp.pairs:
            n_pair = min(len(old_chain), len(cso_chain))
            chain = cso_chain[:n_pair]
            if not chain:
                continue
            # where the ORIGINAL fingertip is at the setup grip
            target = _chain_cloud_old(skel, setup_world_old, sp.hand,
                                      old_chain)[-1]

            local_rot, local_t, axis_local, abd_local = [], [], [], []
            parent_w = hand_w
            for i, c in enumerate(chain):
                w = base_cso[c]
                local_rot.append(parent_w[:3, :3].T @ w[:3, :3])
                local_t.append(asset.bones[c].rest_local[:3, 3])
                # flexion axis: palm-across orthogonalized against the
                # bone direction, expressed in the bone's rest frame
                if i + 1 < len(cso_chain):
                    y = asset.head(cso_chain[i + 1]) - asset.head(c)
                else:
                    y = asset.tail(c) - asset.head(c)
                anat = bone_anat_3x3(y, rig.palm_world[:3, 1])
                axis_local.append(
                    asset.bones[c].rest_world[:3, :3].T @ anat[:, 0])
                # abduction: about the axis normal to the bone and to flexion
                abd_local.append(asset.bones[c].rest_world[:3, :3].T @ anat[:, 2])
                parent_w = w
            leaf = chain[-1]
            tip_local = asset.bones[leaf].rest_world[:3, :3].T \
                @ (asset.tail(leaf) - asset.head(leaf))
            old_leaf = old_chain[n_pair - 1]
            if old_centroids and old_leaf in old_centroids \
                    and leaf in asset.centroids_local:
                target = old_centroids[old_leaf]
                tip_local = asset.centroids_local[leaf]

            def fk(thetas, phis=None):
                out, wp = [], hand_w
                for i in range(len(chain)):
                    m = np.eye(4)
                    m[:3, :3] = local_rot[i] @ axis_angle(axis_local[i],
                                                          thetas[i])
                    if phis is not None and phis[i]:
                        m[:3, :3] = m[:3, :3] @ axis_angle(abd_local[i], phis[i])
                    m[:3, 3] = local_t[i]
                    wp = wp @ m
                    out.append(wp)
                return out

            surface = None
            if old_skin is not None:
                old_tris = [old_skin[b] for b in old_chain if b in old_skin]
                ours_local = [asset.skin_local.get(c) for c in chain]
                if old_tris and all(p is not None and len(p) for p in ours_local):
                    surface = (np.concatenate(old_tris),
                               [p[:: max(1, len(p) // 24)] for p in ours_local])

            # the finger's own TRIANGLES as the engine will draw them (each
            # corner rides its bone rigidly): the web between the thumb's
            # base and the palm belongs to no single bone, and it is what
            # sinks deepest (v_elite: 0.75 u)
            pen = None
            if weapon is not None:
                tri_bones, tri_local = _hand_triangles(asset)
                rows = np.isin(tri_bones, chain).any(axis=1)
                reach = sum(float(np.linalg.norm(t)) for t in local_t) + 2.5
                root = base_cso[chain[0]][:3, 3]
                shell = weapon.within(root - reach, root + reach)
                if rows.any() and len(shell):
                    limits = np.array([
                        max(max(PEN_TOLERANCE, (allowed or {}).get(b, 0.0)) for b in row)
                        for row in tri_bones[rows]])
                    pen = (shell, tri_bones[rows], tri_local[rows], np.tile(limits, 7))

            def pen_err(worlds):
                if pen is None:
                    return 0.0
                shell, bones_of, local_of, limits = pen
                reach = 1.0  # the search moves by degrees: nothing gets deeper unseen
                posed = dict(zip(chain, worlds, strict=True))
                corners = np.empty_like(local_of)
                for name in np.unique(bones_of):
                    m = posed.get(name)
                    if m is None:  # the palm, a neighbour: as posed before the fit
                        m = base_cso.get(name, asset.bones[name].rest_world)
                    sel = bones_of == name
                    corners[sel] = local_of[sel] @ m[:3, :3].T + m[:3, 3]
                a, b, c = corners[:, 0], corners[:, 1], corners[:, 2]
                pts = np.concatenate([a, b, c, (a + b) / 2, (b + c) / 2, (c + a) / 2,
                                      (a + b + c) / 3])
                excess = np.clip(shell.depth(pts, reach) - limits, 0.0, None)
                return PEN_WEIGHT * float(excess.mean())

            def tip_err(worlds):
                return fit_err(worlds) + pen_err(worlds)

            def fit_err(worlds):
                if surface is not None:
                    old_tris_all, pts = surface
                    world_pts = np.concatenate([
                        p @ w[:3, :3].T + w[:3, 3] for p, w in zip(pts, worlds, strict=True)])
                    return float(points_to_mesh(world_pts, old_tris_all).mean())
                w = worlds[-1]
                tip = w[:3, :3] @ tip_local + w[:3, 3]
                return float(np.linalg.norm(tip - target))

            thetas = [0.0] * len(chain)
            phis = None
            base_worlds = fk(thetas)
            err0, pen0 = tip_err(base_worlds), pen_err(base_worlds)

            tail_local = asset.bones[leaf].rest_world[:3, :3].T \
                @ (asset.tail(leaf) - asset.head(leaf))

            def bends(worlds):
                """Signed bends at the middle and the last joint (same sense)."""
                pts = [w[:3, 3] for w in worlds]
                pts.append(worlds[-1][:3, :3] @ tail_local + worlds[-1][:3, 3])
                seg = [normalized(pts[k + 1] - pts[k]) for k in range(3)]
                out = []
                for k in (1, 2):
                    hinge = worlds[k][:3, :3] @ axis_local[k]
                    angle = float(np.arccos(np.clip(seg[k - 1] @ seg[k], -1.0, 1.0)))
                    out.append(angle if np.cross(seg[k - 1], seg[k]) @ hinge >= 0 else -angle)
                return out

            couple = False
            if coupled and len(chain) == 3 and cso_chain is not rig.chains[0]:
                mid0 = bends(base_worlds)[0]
                couple = abs(mid0) > np.radians(10.0)  # a straight finger has no sense yet
                closing = 1.0 if mid0 > 0 else -1.0

            def slave(cand, swing=None):
                """``cand`` with its last joint set to follow the middle one."""
                best = None
                for t in np.arange(-SLAVE_RANGE, SLAVE_RANGE + 1e-9, step):
                    trial = [cand[0], cand[1], float(t)]
                    mid, last = bends(fk(trial, swing))
                    miss = abs(closing * last - DIP_RATIO * max(closing * mid, 0.0))
                    if best is None or miss < best[0] - 1e-9:
                        best = (miss, trial)
                return best[1]

            if couple:
                thetas = slave(thetas)
                for _pass in range(3):
                    for i, lim_i in ((0, max_auto), (1, max(max_auto, COUPLED_MID_RANGE))):
                        def err_coupled(t, i=i):
                            cand = list(thetas)
                            cand[i] = t
                            return tip_err(fk(slave(cand)))
                        thetas[i] = _search(err_coupled, thetas[i], lim_i, step)
                        thetas = slave(thetas)
            elif pen0 > 0 or err0 > (SURFACE_MIN_ERR if surface is not None else min_err):
                for _pass in range(3):        # coordinate descent
                    for i in range(len(chain)):
                        def err_flex(t, i=i):
                            cand = list(thetas)
                            cand[i] = t
                            return tip_err(fk(cand))
                        thetas[i] = _search(err_flex, thetas[i], max_auto, step)
            if couple or pen0 > 0 or err0 > (SURFACE_MIN_ERR if surface is not None
                                             else min_err):
                # fingers also swing sideways (the thumb opposes, the others
                # splay): with a surface to fit, a small abduction per joint
                # after the flexion passes (corpus: grip 0.069 -> 0.064 u)
                if surface is not None or pen is not None:
                    phis = [0.0] * len(chain)
                    is_thumb = cso_chain is rig.chains[0]
                    lim = np.radians(THUMB_ABDUCTION_DEG if pen is not None and is_thumb
                                     else ABDUCTION_DEG)
                    for _pass in range(2):
                        for i in range(len(chain)):
                            def err_abd(t, i=i):
                                cand = list(phis)
                                cand[i] = t
                                return tip_err(fk(thetas, cand))
                            phis[i] = _search(err_abd, phis[i], lim, step)
                    if couple:
                        thetas = slave(thetas, phis)
                    elif pen is not None:  # the swing changed what curl fits
                        for i in range(len(chain)):
                            def err_flex2(t, i=i):
                                cand = list(thetas)
                                cand[i] = t
                                return tip_err(fk(cand, phis))
                            thetas[i] = _search(err_flex2, thetas[i], max_auto, step)

            finger = chain[0].split("Finger")[0].replace("00", "")
            extra = 0.0
            if manual:
                for (m_side, m_finger), deg in manual.items():
                    if m_side == sp.side and \
                            m_finger.lower() in chain[0].lower():
                        extra = np.radians(deg)
            if extra:
                # "+" always closes the finger: the flexion axis is mirrored
                # between the hands (right closes at +, left at -), so take
                # the sign that brings the tip toward the palm centre (half
                # way from the wrist to the finger roots: right for the
                # thumb too, which closes across the palm, not toward the wrist)
                roots = np.mean([base_cso[c[0]][:3, 3] for _o, c in sp.pairs if c], axis=0)
                palm = (hand_w[:3, 3] + roots) / 2.0

                def reach(delta):
                    w = fk([t + delta for t in thetas], phis)[-1]
                    return float(np.linalg.norm(w[:3, :3] @ tip_local + w[:3, 3] - palm))
                closing = 1.0 if reach(np.radians(5)) < reach(-np.radians(5)) else -1.0
                # the manual range does not depend on the automatic clamp
                # (it used to be 1.5 x max_deg: zero under --no-snug)
                lim = max_auto + MANUAL_CURL_RANGE
                thetas = [max(-lim, min(lim, t + closing * extra)) for t in thetas]

            if any(abs(t) > 1e-6 for t in thetas) or (phis and any(phis)):
                worlds = fk(thetas, phis)
                for i in range(len(chain)):
                    new_k[chain[i]] = (
                        setup_world_old[old_chain[i]][:3, :3].T
                        @ worlds[i][:3, :3])
                log("  snug %s %-6s %s deg (tip err %.2f -> %.2f%s)"
                    % (sp.side, finger,
                       "/".join("%+.0f" % np.degrees(t) for t in thetas),
                       err0 - pen0, fit_err(worlds),
                       "" if pen is None else "; in weapon %.3f -> %.3f"
                       % (pen0 / PEN_WEIGHT, pen_err(worlds) / PEN_WEIGHT)))

        if new_k:
            sp.finger_k = [(o, c, new_k.get(c, k))
                           for o, c, k in sp.finger_k]


# The view in SMD space: eye at the origin, forward -Y, up +Z (studiomdl
# turns SMD data +90 deg about Z into the engine's +X-forward model space).
# A shoulder must stay out of it: widescreen Hor+ shows ~106 deg across.
VIEW_NEAR = 4.0  # the engine's near clip
VIEW_TAN_H = float(np.tan(np.radians(56.0)))
VIEW_TAN_V = float(np.tan(np.radians(41.0)))
SHOULDER_RADIUS = 3.0  # the upper arm's thickness, and the cap past its head
SHOULDER_ANCHOR = np.array([8.0, 3.0, -10.0])  # the player's shoulder (x mirrored a side)
MAX_SHOULDER_BEND = float(np.radians(60.0))  # sharper folds the elbow's skin over


MAX_EXTRA_TURN = 12.0  # deg a frame an arm part may turn beyond the original's turn


def _angle(a: np.ndarray, b: np.ndarray) -> float:
    """Degrees between two unit vectors."""
    return float(np.degrees(np.arccos(np.clip(np.dot(a, b), -1.0, 1.0))))


def _limit_turn(previous: np.ndarray, wanted: np.ndarray, limit_deg: float) -> np.ndarray:
    """``wanted``, turned from ``previous`` by at most ``limit_deg``."""
    angle = _angle(previous, wanted)
    if angle <= limit_deg or angle < 1e-9:
        return wanted
    return _slerp_dir(previous, wanted, limit_deg / angle)


def reset_motion(plan: RetargetPlan) -> None:
    """Start a new sequence: no frame-to-frame continuity across sequences."""
    for sp in plan.sides:
        sp._prev_orig = sp._prev_d = sp._prev_u = None


def _on_screen(point: np.ndarray) -> bool:
    depth = -point[1]
    return bool(depth > VIEW_NEAR and abs(point[0]) < depth * VIEW_TAN_H
                and abs(point[2]) < depth * VIEW_TAN_V)


ARM_RADIUS = 2.5  # an arm's thickness about its bones


def _segment_seen(start: np.ndarray, d: np.ndarray, length: float,
                  fractions: tuple[float, ...]) -> bool:
    """Any of an arm part (its axis and skin around it) on screen."""
    helper = np.array([0.0, 0.0, 1.0]) if abs(d[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    a = normalized(np.cross(d, helper))
    b = np.cross(d, a)
    for f in fractions:
        p = start + d * length * f
        if any(_on_screen(q) for q in (p, p + ARM_RADIUS * a, p - ARM_RADIUS * a,
                                       p + ARM_RADIUS * b, p - ARM_RADIUS * b)):
            return True
    return False


def _forearm_seen(wrist: np.ndarray, d: np.ndarray, length: float) -> bool:
    """Any of the forearm's elbow half (the wrist end is the hand's) on screen."""
    return _segment_seen(wrist, d, length, (0.4, 0.55, 0.7, 0.85, 1.0))


def _keep_forearm_hidden(wrist: np.ndarray, d: np.ndarray, d_orig: np.ndarray,
                         length: float) -> np.ndarray:
    """``d`` (the natural forearm), turned back toward the original one just
    as far as it takes to leave the view — when the original forearm was out
    of it and ours would come in (v_c4's draw: the hands rise from below the
    screen, our forearm and elbow were already up in view)."""
    if not _forearm_seen(wrist, d, length) or _forearm_seen(wrist, d_orig, length):
        return d
    low, high = 0.0, 1.0  # the least turn back that hides it
    for _ in range(16):
        mid = (low + high) / 2
        if _forearm_seen(wrist, _slerp_dir(d, d_orig, mid), length):
            low = mid
        else:
            high = mid
    return _slerp_dir(d, d_orig, high)


def _shoulder_cap_seen(shoulder: np.ndarray, up_dir: np.ndarray) -> bool:
    """Any of the shoulder's cap (head, past it, around it) on screen."""
    helper = np.array([0.0, 0.0, 1.0]) if abs(up_dir[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    a = normalized(np.cross(up_dir, helper))
    b = np.cross(up_dir, a)
    points = [shoulder, shoulder + up_dir * SHOULDER_RADIUS]
    points += [shoulder + SHOULDER_RADIUS * v for v in (a, -a, b, -b)]
    return any(_on_screen(p) for p in points)


def _slerp_dir(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    angle = float(np.arccos(np.clip(np.dot(a, b), -1.0, 1.0)))
    if angle < 1e-6:
        return a
    return normalized((np.sin((1 - t) * angle) * a + np.sin(t * angle) * b)
                      / np.sin(angle))


def _hidden_shoulder(elbow: np.ndarray, d: np.ndarray, upper_len: float,
                     prefer: np.ndarray | None = None, whole: bool = False) -> np.ndarray:
    """The shoulder for an elbow: straight on along the forearm line ``d``
    (elbow -> shoulder) when that is off screen — else the upper arm bends at
    the elbow just as far as it takes to leave the view: the least bend of
    those toward the player's own shoulder (behind the eye, below, on the
    arm's side), straight down, outward and back. Bone lengths stay (hand
    bones only rotate); the forearm and hand — the grip — do not move.
    ``whole``: the original arm is out of view, so all of the upper arm
    must be, not only the shoulder."""

    def _shoulder_seen(shoulder: np.ndarray, up_dir: np.ndarray) -> bool:
        if whole and _segment_seen(elbow, up_dir, upper_len, (0.0, 0.25, 0.5, 0.75)):
            return True
        return _shoulder_cap_seen(shoulder, up_dir)

    shoulder = elbow + d * upper_len
    if not _shoulder_seen(shoulder, d):
        return shoulder
    side = 1.0 if elbow[0] >= 0 else -1.0
    targets = [SHOULDER_ANCHOR * np.array([side, 1.0, 1.0]) - elbow,
               np.array([0.0, 0.0, -1.0]), np.array([side, 0.0, 0.0]),
               np.array([0.0, 1.0, 0.0]), np.array([side, 0.0, -1.0])]
    best: tuple[float, np.ndarray] | None = None
    fallback = None
    for target in targets:
        if np.linalg.norm(target) < 1e-6:
            continue
        toward = normalized(target)
        span = float(np.arccos(np.clip(np.dot(d, toward), -1.0, 1.0)))
        limit = 1.0 if span <= MAX_SHOULDER_BEND else MAX_SHOULDER_BEND / span
        toward = _slerp_dir(d, toward, limit)  # no further than the elbow folds
        fallback = fallback if fallback is not None else toward
        if _shoulder_seen(elbow + toward * upper_len, toward):
            continue
        low, high = 0.0, 1.0  # least bend toward this that hides it
        for _ in range(16):
            mid = (low + high) / 2
            u = _slerp_dir(d, toward, mid)
            if _shoulder_seen(elbow + u * upper_len, u):
                low = mid
            else:
                high = mid
        u = _slerp_dir(d, toward, high)
        # the least bend, or (mid-sequence) the way nearest last frame's
        angle = _angle(u, prefer if prefer is not None else d)
        if best is None or angle < best[0]:
            best = (angle, u)
    if best is not None:
        return elbow + best[1] * upper_len
    # nothing within the fold hides it: as far toward the player's shoulder
    # as the elbow folds (most of the cap leaves the view)
    return elbow + (fallback if fallback is not None else d) * upper_len


def solve_frame(plan: RetargetPlan,
                world: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """World matrices for every driven CSO bone at one frame.

    `world` = original skeleton world matrices (by name) at this frame.
    """
    asset = plan.asset
    out: dict[str, np.ndarray] = {}
    for sp in plan.sides:
        rig = sp.rig
        hand_w = world[sp.hand.wrist] @ sp.c_hand
        w_target = hand_w[:3, 3]

        # original forearm direction (wrist -> elbow), per frame
        w_orig = world[sp.hand.wrist][:3, 3]
        if sp.elbow_ref is not None:
            d = world[sp.elbow_ref][:3, 3] - w_orig
        else:
            d = FALLBACK_ARM_DIR.copy()
        if np.linalg.norm(d) < 1e-3:
            d = sp._prev_dir if sp._prev_dir is not None \
                else FALLBACK_ARM_DIR.copy()
        d = normalized(d)
        sp._prev_dir = d
        d_orig = d
        fore_len = float(np.linalg.norm(asset.head(rig.wrist) - asset.head(rig.arm[1])))
        # the original's whole arm out of view (a draw rising from below the
        # screen) — then ours stays out too; not when only its forearm is
        # (a reload's: switching there would jerk the arm about)
        arm_hidden = (not _on_screen(w_target)
                      and not _forearm_seen(w_target, d_orig, fore_len))
        if NATURAL_WRIST:
            palm_now = hand_w @ rig.palm_local
            palm_rest = rig.palm_world
            back_rest = normalized(asset.head(rig.arm[1]) - asset.head(rig.wrist))
            d = _natural_arm(palm_now, d, _palm_angles(palm_rest, back_rest), sp.side)
            if arm_hidden:
                d = _keep_forearm_hidden(w_target, d, d_orig, fore_len)
        # continuity: the forearm turns no faster than the original one (plus
        # a margin) — the wrist-angle clamp could flip it in one frame when
        # the original wrist folded past 90 deg (v_holysword: 7.8 u elbow jump)
        if sp._prev_d is not None and sp._prev_orig is not None:
            d = _limit_turn(sp._prev_d, d, _angle(sp._prev_orig, d_orig) + MAX_EXTRA_TURN)
        turned = _angle(sp._prev_d, d) if sp._prev_d is not None else None
        sp._prev_orig, sp._prev_d = d_orig, d

        upper, fore0, fore1 = rig.arm  # UpperArm, Arm0, Arm1
        s_rest = asset.head(upper)
        e_rest = asset.head(fore0)
        w_rest = asset.head(rig.wrist)
        fore_chord = float(np.linalg.norm(w_rest - e_rest))
        upper_len = float(np.linalg.norm(e_rest - s_rest))

        elbow = w_target + d * fore_chord
        shoulder = (_hidden_shoulder(elbow, d, upper_len, sp._prev_u, whole=arm_hidden)
                    if HIDE_SHOULDERS
                    else w_target + d * (fore_chord + upper_len))
        u = normalized(shoulder - elbow)
        if sp._prev_u is not None and turned is not None:
            # the upper arm turns no faster than the forearm (plus a margin):
            # no shoulder jumping between ways out of the view
            u = _limit_turn(sp._prev_u, u, turned + MAX_EXTRA_TURN)
            shoulder = elbow + u * upper_len
        sp._prev_u = u

        # forearm assembly: aim the elbow->wrist chord, then take the
        # twist of the desired hand rotation about the chord
        chord_axis = normalized(w_target - elbow)
        r_aim = rotation_between(w_rest - e_rest, w_target - elbow)
        hand_rest_rot = asset.bones[rig.wrist].rest_world[:3, :3]
        residual = hand_w[:3, :3] @ (r_aim @ hand_rest_rot).T
        tau = twist_angle(residual, chord_axis)
        r_fore = axis_angle(chord_axis, tau) @ r_aim
        t_fore = compose(r_fore, elbow - r_fore @ e_rest)
        out[fore0] = t_fore @ asset.bones[fore0].rest_world
        out[fore1] = t_fore @ asset.bones[fore1].rest_world

        # upper arm: aim shoulder->elbow, roll follows the forearm
        r_up_aim = rotation_between(e_rest - s_rest, elbow - shoulder)
        res_up = r_fore @ r_up_aim.T
        tau_up = twist_angle(res_up, normalized(elbow - shoulder))
        r_up = axis_angle(normalized(elbow - shoulder), tau_up) @ r_up_aim
        t_up = compose(r_up, shoulder - r_up @ s_rest)
        out[upper] = t_up @ asset.bones[upper].rest_world

        # hand: exactly the desired grip
        out[rig.wrist] = hand_w

        # fingers: constant anatomical offset from the paired old bone,
        # translations FK-inherited (rest locals)
        for o, c, k in sp.finger_k:
            parent = asset.bones[c].parent
            p_world = out[parent]
            rot = world[o][:3, :3] @ k
            head = p_world @ np.append(
                asset.bones[c].rest_local[:3, 3], 1.0)
            out[c] = compose(rot, head[:3])

        # unpaired chain bones (old chain shorter, or a whole CSO finger
        # without an original counterpart): ride the parent at rest local
        for cc in rig.chains:
            for c in cc:
                if c not in out:
                    out[c] = out[asset.bones[c].parent] \
                        @ asset.bones[c].rest_local

        for extra in rig.extras:
            out[extra] = out[asset.bones[extra].parent] \
                @ asset.bones[extra].rest_local
    return out
