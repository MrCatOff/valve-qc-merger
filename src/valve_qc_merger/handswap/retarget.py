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
from .asset import HandsAsset, SideRig
from .identify import HandInfo, OrigSkeleton
from .math3d import (axis_angle, compose, inv_rigid, normalized,
                     rotation_between, twist_angle)

FALLBACK_ARM_DIR = np.array([0.0, 8.0, -6.0])  # behind and below the view


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
    arm_override: np.ndarray | None = None     # fixed arm dir off the weapon
    _prev_dir: np.ndarray | None = None


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


def build_side_plan(asset: HandsAsset, skel: OrigSkeleton, hand: HandInfo,
                    setup_world: dict[str, np.ndarray], log=print,
                    grip_offset=None) -> SidePlan:
    """grip_offset: optional (dx, dy, dz) in PALM-FRAME axes (X fingers-
    forward, Y across the palm toward the thumb, Z the palm normal) that
    shifts where the CSO palm lands relative to the original palm —
    the per-weapon tuning knob for how the weapon sits in the hand."""
    rig = asset.sides[hand.side]
    l_cso = rig.palm_local
    l_cso_inv = inv_rigid(l_cso)
    if grip_offset is not None:
        shift = np.eye(4)
        shift[:3, 3] = grip_offset
        l_cso_inv = shift @ l_cso_inv

    cso_clouds = [_chain_cloud_cso(asset, rig, ch) for ch in rig.chains]
    old_clouds = [_chain_cloud_old(skel, setup_world, hand, ch)
                  for ch in hand.chains]
    w = setup_world[hand.wrist][:3, 3]
    old_roots = [setup_world[ch[0]][:3, 3] for ch in hand.chains]
    hand_rest = asset.bones[rig.wrist].rest_world

    n = min(len(hand.chains), len(rig.chains))
    best = None
    for thumb_i in range(len(hand.chains)):
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
            for perm in itertools.permutations(range(len(pred)), n):
                total = sum(cost[i, perm[i]] for i in range(n))
                if best is None or total < best[0]:
                    best = (total, thumb_i, perm, desired, a_old)

    total, thumb_i, perm, desired, a_old = best
    pairs = [(hand.chains[i], rig.chains[perm[i]]) for i in range(n)]
    log("  %s: thumb %r (fit %.2f), pairing %s"
        % (hand.side, hand.chains[thumb_i][0], total / max(n, 1),
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
               arm_dirs: dict | None = None) -> RetargetPlan:
    """``arm_dirs`` {side: [x,y,z]} pins the forearm/elbow direction for a side —
    used to point the arm toward the player on weapons whose original wrist-parent
    bone runs along the barrel (the arm would otherwise reach into the muzzle)."""
    plan = RetargetPlan(asset=asset)
    for hand in hands:
        if hand.side not in asset.sides:
            log("  WARNING: no CSO rig for side %r — skipped" % hand.side)
            continue
        off = (grip_offsets or {}).get(hand.side)
        sp = build_side_plan(asset, skel, hand, setup_world, log, grip_offset=off)
        ov = (arm_dirs or {}).get(hand.side)
        if ov is not None:
            sp.arm_override = normalized(np.asarray(ov, dtype=float))
            log("  %s: arm direction pinned to %s (toward the player)"
                % (hand.side, np.round(sp.arm_override, 2)))
        plan.sides.append(sp)
    return plan


def refine_finger_fit(plan: RetargetPlan, skel: OrigSkeleton,
                      setup_world_old: dict[str, np.ndarray],
                      *, max_deg: float = 18.0, min_err: float = 0.35,
                      manual=None, log=print):
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

    manual: {(side, finger_prefix): extra_degrees} applied per joint on
    top of the automatic result (still clamped)."""
    asset = plan.asset
    step = np.radians(2.0)
    max_auto = np.radians(max_deg)
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

            local_rot, local_t, axis_local = [], [], []
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
                ax_world = bone_anat_3x3(y, rig.palm_world[:3, 1])[:, 0]
                axis_local.append(
                    asset.bones[c].rest_world[:3, :3].T @ ax_world)
                parent_w = w
            leaf = chain[-1]
            tip_local = asset.bones[leaf].rest_world[:3, :3].T \
                @ (asset.tail(leaf) - asset.head(leaf))

            def fk(thetas):
                out, wp = [], hand_w
                for i in range(len(chain)):
                    m = np.eye(4)
                    m[:3, :3] = local_rot[i] @ axis_angle(axis_local[i],
                                                          thetas[i])
                    m[:3, 3] = local_t[i]
                    wp = wp @ m
                    out.append(wp)
                return out

            def tip_err(worlds):
                w = worlds[-1]
                tip = w[:3, :3] @ tip_local + w[:3, 3]
                return float(np.linalg.norm(tip - target))

            thetas = [0.0] * len(chain)
            err0 = tip_err(fk(thetas))
            if err0 > min_err:
                for _pass in range(3):        # coordinate descent
                    for i in range(len(chain)):
                        best = (tip_err(fk(thetas)), thetas[i])
                        t = -max_auto
                        while t <= max_auto + 1e-9:
                            cand = list(thetas)
                            cand[i] = t
                            e = tip_err(fk(cand))
                            # prefer the smaller curl on near-ties
                            if e < best[0] - 1e-4 or \
                                    (e < best[0] + 1e-4
                                     and abs(t) < abs(best[1])):
                                best = (e, t)
                            t += step
                        thetas[i] = best[1]

            finger = chain[0].split("Finger")[0].replace("00", "")
            extra = 0.0
            if manual:
                for (m_side, m_finger), deg in manual.items():
                    if m_side == sp.side and \
                            m_finger.lower() in chain[0].lower():
                        extra = np.radians(deg)
            if extra:
                lim = max_auto * 1.5
                thetas = [max(-lim, min(lim, t + extra)) for t in thetas]

            if any(abs(t) > 1e-6 for t in thetas):
                worlds = fk(thetas)
                for i in range(len(chain)):
                    new_k[chain[i]] = (
                        setup_world_old[old_chain[i]][:3, :3].T
                        @ worlds[i][:3, :3])
                log("  snug %s %-6s %s deg (tip err %.2f -> %.2f)"
                    % (sp.side, finger,
                       "/".join("%+.0f" % np.degrees(t) for t in thetas),
                       err0, tip_err(worlds)))

        if new_k:
            sp.finger_k = [(o, c, new_k.get(c, k))
                           for o, c, k in sp.finger_k]


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
        if sp.arm_override is not None:
            d = sp.arm_override            # arm would pierce the weapon: fixed off it
        elif sp.elbow_ref is not None:
            d = world[sp.elbow_ref][:3, 3] - w_orig
        else:
            d = FALLBACK_ARM_DIR.copy()
        if np.linalg.norm(d) < 1e-3:
            d = sp._prev_dir if sp._prev_dir is not None \
                else FALLBACK_ARM_DIR.copy()
        d = normalized(d)
        sp._prev_dir = d

        upper, fore0, fore1 = rig.arm  # UpperArm, Arm0, Arm1
        s_rest = asset.head(upper)
        e_rest = asset.head(fore0)
        w_rest = asset.head(rig.wrist)
        fore_chord = float(np.linalg.norm(w_rest - e_rest))
        upper_len = float(np.linalg.norm(e_rest - s_rest))

        elbow = w_target + d * fore_chord
        shoulder = w_target + d * (fore_chord + upper_len)

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
