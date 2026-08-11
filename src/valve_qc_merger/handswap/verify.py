"""Verification: reproduce GoldSrc skinning on the OUTPUT files and check
them against the original model.

Everything is measured from the files on disk (re-parsed), not from
in-memory state — this validates the writers, the euler round-trip and the
merged node table exactly as studiomdl will see them.

Checks:
  1. every sequence exists, frame counts match the original;
  2. kept weapon bones reproduce the ORIGINAL world-space trajectories
     (never compare local values — re-parented bones re-express them);
  3. CSO hand bones are rotation-only (constant local positions) and keep
     constant bone lengths — the shape of Valve's own viewmodel data;
  4. the CSO wrist tracks the original wrist rigidly (grip never drifts);
  5. engine skinning v(t) = M(t) B^-1 v_bind of the output hands mesh:
     triangle-edge stretch is compared against the SAME metric computed on
     the original hands — the baseline makes the threshold model-relative;
  6. the output QC only references surviving bones.
"""
from __future__ import annotations

import os

import numpy as np

from . import build as buildmod
from . import qc as qcmod
from . import smd as smdmod
from .math3d import inv_rigid, rotation_angle

POS_TOL = 0.05          # units, world-space trajectory reproduction
ROT_TOL_DEG = 0.5
STRETCH_MAX_HARD = 3.5  # absolute explosion guard
STRETCH_BASELINE_FACTOR = 1.6


def _world_by_name(smd: smdmod.Smd, frame: int) -> dict[str, np.ndarray]:
    n_of = smd.name_of()
    return {n_of[i]: m for i, m in smd.world_matrices(frame).items()}


class _MeshSkin:
    """Vectorized rigid skinning of one reference SMD."""

    def __init__(self, ref: smdmod.Smd):
        self.n_of = ref.name_of()
        pos, bones = [], []
        for tri in ref.triangles:
            for v in tri.verts:
                pos.append(v.pos)
                bones.append(v.dominant_bone())
        self.pos = np.array(pos) if pos else np.zeros((0, 3))
        self.bones = np.array(bones, dtype=int) if bones else np.zeros(0, int)
        n = len(self.pos)
        tri_idx = np.arange(n).reshape(-1, 3)
        e = np.concatenate([tri_idx[:, [0, 1]], tri_idx[:, [1, 2]],
                            tri_idx[:, [2, 0]]])
        self.edges = e
        self.bind_inv = {i: inv_rigid(m)
                         for i, m in ref.bind_matrices().items()}
        bind_pos = self.skin(_world_by_name(ref, 0))
        self.bind_len = np.linalg.norm(bind_pos[e[:, 0]] - bind_pos[e[:, 1]],
                                       axis=1)
        # only edges that CAN deform (crossing bones) are interesting
        self.cross = self.bones[e[:, 0]] != self.bones[e[:, 1]]

    def skin(self, world_by_name: dict[str, np.ndarray]) -> np.ndarray:
        out = np.empty_like(self.pos)
        for b in np.unique(self.bones):
            name = self.n_of[b]
            m = world_by_name[name] @ self.bind_inv[b]
            idx = self.bones == b
            out[idx] = self.pos[idx] @ m[:3, :3].T + m[:3, 3]
        return out

    def stretch_stats(self, frames_world: list[dict[str, np.ndarray]]):
        """Edge deformation over all frames, cross-bone edges only.

        Tiny joint-crossing edges produce huge but invisible ratios, so the
        ratio is only taken on edges of meaningful bind length; tiny edges
        are covered by the absolute excess-length metric instead."""
        if not self.cross.any() or not len(self.pos):
            return {"ratio_max": 1.0, "p95": 1.0, "excess_max": 0.0}
        e, ln = self.edges[self.cross], self.bind_len[self.cross]
        ln = np.maximum(ln, 1e-6)
        big = ln > 0.5
        mx, excess, p95s = 1.0, 0.0, [1.0]
        for w in frames_world:
            p = self.skin(w)
            d = np.linalg.norm(p[e[:, 0]] - p[e[:, 1]], axis=1)
            excess = max(excess, float((d - ln).max()))
            ratio = d / ln
            if big.any():
                mx = max(mx, float(ratio[big].max()))
                p95s.append(float(np.percentile(ratio[big], 95)))
        return {"ratio_max": mx, "p95": max(p95s), "excess_max": excess}


def verify(weapon_dir: str, out_dir: str, model, plan, asm,
           log=print) -> dict:
    errors, warnings = [], []
    metrics: dict = {}

    out_qc = qcmod.parse(os.path.join(out_dir,
                                      os.path.basename(model.qc.path)))
    out_refs = {}
    for ref in {s for _n, s in out_qc.references}:
        base = ref.split("/")[-1]
        p = os.path.join(out_dir, base + ".smd")
        if not os.path.isfile(p):
            errors.append("reference %r missing" % base)
            continue
        out_refs[base] = smdmod.parse(p)

    # ---- QC bone references ------------------------------------------
    survivors = asm.survivors
    for att in out_qc.attachments:
        if att["bone"] not in survivors:
            errors.append("$attachment %d on dead bone %r"
                          % (att["idx"], att["bone"]))
    for b in out_qc.hboxes:
        if b not in survivors:
            errors.append("$hbox on dead bone %r" % b)
    for b in out_qc.controllers:
        if b not in survivors:
            errors.append("$controller on dead bone %r" % b)

    # ---- per-sequence checks -----------------------------------------
    cso_set = set(asm.cso_bones)
    kept_set = set(asm.kept_weapon)
    grip_offsets = {sp.rig.wrist: (sp.hand.wrist, sp.c_hand)
                    for sp in plan.sides}
    max_pos_err = max_rot_err = 0.0
    max_grip_pos = max_grip_rot = 0.0
    max_hand_pos_range = 0.0
    max_len_dev = 0.0
    out_seq_worlds: list[dict] = []   # all frames of all sequences

    for seq in out_qc.sequences:
        rel = seq["smd"]
        path = os.path.join(out_dir, rel + ".smd")
        if not os.path.isfile(path):
            errors.append("sequence SMD missing: %s" % rel)
            continue
        out_smd = smdmod.parse(path)
        orig_smd = model.anims.get(rel)
        if orig_smd is None:
            warnings.append("no original for sequence %s" % rel)
            continue
        if len(out_smd.frames) != len(orig_smd.frames):
            errors.append("frame count %s: %d != %d (events will desync)"
                          % (rel, len(out_smd.frames), len(orig_smd.frames)))
            continue

        orig_worlds = buildmod.anim_world_frames(orig_smd, model.skel)
        id_of = out_smd.id_of()

        # 3) rotation-only hand channels (roots excluded: they carry the
        # arm's translation, like the original models' root bones)
        for name in cso_set:
            if asm.parent_name[name] is None:
                continue
            i = id_of[name]
            pts = np.array([fr[i][0] for fr in out_smd.frames])
            rng = float(np.ptp(pts, axis=0).max()) if len(pts) > 1 else 0.0
            max_hand_pos_range = max(max_hand_pos_range, rng)

        for t in range(len(out_smd.frames)):
            w_out = _world_by_name(out_smd, t)
            out_seq_worlds.append(w_out)
            w_orig = orig_worlds[t]
            # 2) weapon trajectories
            for name in kept_set:
                dp = np.linalg.norm(w_out[name][:3, 3]
                                    - w_orig[name][:3, 3])
                da = rotation_angle(w_out[name][:3, :3]
                                    @ w_orig[name][:3, :3].T)
                max_pos_err = max(max_pos_err, float(dp))
                max_rot_err = max(max_rot_err, float(np.degrees(da)))
            # 4) grip tracking
            for wrist, (old_wrist, c_hand) in grip_offsets.items():
                want = w_orig[old_wrist] @ c_hand
                have = w_out[wrist]
                max_grip_pos = max(max_grip_pos, float(
                    np.linalg.norm(want[:3, 3] - have[:3, 3])))
                max_grip_rot = max(max_grip_rot, float(np.degrees(
                    rotation_angle(want[:3, :3] @ have[:3, :3].T))))
            # 3b) constant bone lengths within the CSO rig
            for name in cso_set:
                parent = asm.parent_name[name]
                if parent is None:
                    continue
                d = np.linalg.norm(w_out[name][:3, 3]
                                   - w_out[parent][:3, 3])
                rest = np.linalg.norm(
                    plan.asset.bones[name].rest_local[:3, 3])
                max_len_dev = max(max_len_dev, abs(float(d) - float(rest)))

    metrics["weapon_traj_pos_err"] = max_pos_err
    metrics["weapon_traj_rot_err_deg"] = max_rot_err
    metrics["grip_pos_err"] = max_grip_pos
    metrics["grip_rot_err_deg"] = max_grip_rot
    metrics["hand_pos_channel_range"] = max_hand_pos_range
    metrics["hand_bone_len_dev"] = max_len_dev
    if max_pos_err > POS_TOL or max_rot_err > ROT_TOL_DEG:
        errors.append("weapon trajectory mismatch: %.4f units / %.3f deg"
                      % (max_pos_err, max_rot_err))
    if max_grip_pos > POS_TOL or max_grip_rot > ROT_TOL_DEG:
        errors.append("grip drift: %.4f units / %.3f deg"
                      % (max_grip_pos, max_grip_rot))
    if max_hand_pos_range > 1e-2:
        errors.append("hand bones have positional animation (%.4f)"
                      % max_hand_pos_range)
    if max_len_dev > 1e-2:
        errors.append("hand bone lengths deviate by %.4f" % max_len_dev)

    # ---- 5) mesh skinning quality vs original baseline ----------------
    hands_ref = out_refs.get("hands")
    if hands_ref is not None and out_seq_worlds:
        stats = _MeshSkin(hands_ref).stretch_stats(out_seq_worlds)
        metrics["hands_stretch"] = stats

        base = {"ratio_max": 1.0, "p95": 1.0, "excess_max": 0.0}
        orig_frames = []
        for anim in model.anims.values():
            orig_frames.extend(buildmod.anim_world_frames(anim, model.skel))
        baseline_meshes = model._dropped or list(model.refs)
        for mesh in baseline_meshes:
            s = _MeshSkin(model.refs[mesh]).stretch_stats(orig_frames)
            base["ratio_max"] = max(base["ratio_max"], s["ratio_max"])
            base["p95"] = max(base["p95"], s["p95"])
            base["excess_max"] = max(base["excess_max"], s["excess_max"])
        metrics["original_stretch_baseline"] = base

        ratio_limit = max(STRETCH_MAX_HARD,
                          base["ratio_max"] * STRETCH_BASELINE_FACTOR)
        excess_limit = max(2.0,
                           base["excess_max"] * STRETCH_BASELINE_FACTOR)
        if stats["ratio_max"] > ratio_limit:
            errors.append("hands mesh stretch %.2fx (original %.2fx, "
                          "limit %.2fx) — mesh is distorting"
                          % (stats["ratio_max"], base["ratio_max"],
                             ratio_limit))
        if stats["excess_max"] > excess_limit:
            errors.append("hands edge grows by %.2f units (original %.2f, "
                          "limit %.2f) — mesh is tearing"
                          % (stats["excess_max"], base["excess_max"],
                             excess_limit))
        elif stats["ratio_max"] > base["ratio_max"] * STRETCH_BASELINE_FACTOR:
            warnings.append("hands stretch %.2fx above original %.2fx"
                            % (stats["ratio_max"], base["ratio_max"]))

    ok = not errors
    for e in errors:
        log("  VERIFY ERROR: %s" % e)
    for w in warnings:
        log("  verify warning: %s" % w)
    log("Verification %s — traj %.4f u / %.3f deg, grip %.4f u / %.3f deg, "
        "stretch %s"
        % ("OK" if ok else "FAILED", max_pos_err, max_rot_err,
           max_grip_pos, max_grip_rot, metrics.get("hands_stretch")))
    return {"ok": ok, "errors": errors, "warnings": warnings,
            "metrics": metrics}
