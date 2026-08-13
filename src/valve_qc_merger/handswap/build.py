"""Assemble the output model: merged skeleton, reference SMDs, sequences.

Output conventions:
  * One merged nodes table shared by EVERY exported SMD (references and
    sequences) — bone name mismatches are how animations silently detach.
  * The output bind pose: weapon bones keep their original bind worlds
    (so weapon mesh coordinates pass through untouched); CSO hand bones
    are bound at the setup grip (frame 0 of the first sequence), and the
    hands mesh is re-skinned into that pose — self-consistent by
    construction, which is what prevents mesh distortion (skinning error
    is M(t) @ B^-1 amplifying any bind/animation mismatch).
  * Weapon bones re-parented to their nearest surviving ancestor; their
    keys are ALWAYS re-derived as world(parent)^-1 @ world(bone) from the
    original file's own hierarchy — exact for unchanged chains, correct
    for re-rooted ones.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import numpy as np

from . import qc as qcmod
from . import smd as smdmod
from .asset import HandsAsset
from .identify import (HandInfo, OrigSkeleton, find_hands,
                       mesh_bone_weights, merge_skeleton)
from .math3d import inv_rigid, local_matrix, mat3_to_euler, orthonormalize
from .retarget import RetargetPlan, build_plan, solve_frame
from .smd import Node, Smd, Triangle, Vertex

HAND_WEIGHT_FRACTION = 0.95   # mesh is "a hand" if this much weight mass
                              # sits on hand bones


@dataclass
class WeaponModel:
    qc: qcmod.QcInfo
    refs: dict[str, Smd]               # studio name -> parsed reference
    anims: dict[str, Smd]              # qc-relative smd path -> parsed anim
    skel: OrigSkeleton
    weights: dict[str, dict[str, float]]
    hands: list[HandInfo]


@dataclass
class Assembly:
    nodes: list[Node]
    id_of: dict[str, int]
    parent_name: dict[str, str | None]
    cso_bones: list[str]
    kept_weapon: list[str]
    deleted: set[str]
    setup_world_cso: dict[str, np.ndarray]
    setup_world_old: dict[str, np.ndarray]
    dropped_meshes: list[str]
    survivors: set[str] = field(default_factory=set)


def load_weapon(weapon_dir: str, qc_path: str | None, log=print) -> WeaponModel:
    if qc_path is None:
        qcs = [f for f in os.listdir(weapon_dir) if f.lower().endswith(".qc")]
        if len(qcs) != 1:
            raise FileNotFoundError(
                "expected exactly one .qc in %s, found %s" % (weapon_dir, qcs))
        qc_path = os.path.join(weapon_dir, qcs[0])
    qc = qcmod.parse(qc_path)
    log("QC: %s — %d references, %d sequences"
        % (os.path.basename(qc_path), len(qc.references), len(qc.sequences)))

    refs: dict[str, Smd] = {}
    for _group, studio in qc.references:
        base = studio.replace("\\", "/").split("/")[-1]
        p = os.path.join(weapon_dir, base + ".smd")
        if not os.path.isfile(p):
            p = os.path.join(weapon_dir, base)
        if base not in refs:
            refs[base] = smdmod.parse(p)
    anims: dict[str, Smd] = {}
    for seq in qc.sequences:
        rel = seq["smd"]
        if rel in anims:
            continue
        p = os.path.join(weapon_dir, rel + ".smd")
        if not os.path.isfile(p):
            p = os.path.join(weapon_dir, rel)
        anims[rel] = smdmod.parse(p)

    skel = merge_skeleton(refs)
    weights = {name: mesh_bone_weights(s) for name, s in refs.items()}
    log("Merged original skeleton: %d bones" % len(skel.names))
    hand_labeled = {studio.replace("\\", "/").split("/")[-1]
                    for group, studio in qc.references
                    if re.search("hand", group, re.IGNORECASE)}
    hands = find_hands(skel, refs, weights, hand_labeled, log=log)
    if not hands:
        raise RuntimeError("no hands found in the original model")
    return WeaponModel(qc=qc, refs=refs, anims=anims, skel=skel,
                       weights=weights, hands=hands)


# --------------------------------------------------------------------------
# original-skeleton animation sampling


def anim_world_frames(anim: Smd, skel: OrigSkeleton) -> list[dict[str, np.ndarray]]:
    """Per frame: original bone name -> world matrix. Uses the anim file's
    OWN node table for FK (per-file local keys are meaningless under any
    other hierarchy); reference-only bones hold bind locals under their
    reference parent."""
    n_of = anim.name_of()
    out = []
    for t in range(len(anim.frames)):
        by_id = anim.world_matrices(t)
        w = {n_of[i]: m for i, m in by_id.items()}

        def fill(name: str) -> np.ndarray:
            if name in w:
                return w[name]
            pos, rot = skel.bind_local[name]
            m = local_matrix(pos, rot)
            p = skel.parent[name]
            if p is not None:
                m = fill(p) @ m
            w[name] = m
            return m

        for name in skel.names:
            fill(name)
        out.append(w)
    return out


# --------------------------------------------------------------------------
# bone bookkeeping


def hand_bone_set(model: WeaponModel, log=print) -> set[str]:
    """All original bones owned by the hands: the palm cores plus arm
    ancestors that have no weighted descendants outside the hand set."""
    skel = model.skel
    total = {}
    for wmap in model.weights.values():
        for b, x in wmap.items():
            total[b] = total.get(b, 0.0) + x

    # a weapon subtree can slip into the finger chains with low weight
    # and small extent (nexon f2000: the receiver bone became a 'thumb').
    # Its TEXTURE gives it away: finger skin is hand-textured.
    mats = _bone_materials(model)
    from .identify import rebuild_core
    # anchor the material census on NAME-CONFIRMED hands when any exist:
    # a fake fan (kart body) in the union would launder its own texture
    # into a 'hand material'
    anchor = [h for h in model.hands
              if "hand" in h.wrist.lower() or "hand" in h.fan.lower()]
    hand_mats0 = hand_materials(
        model, set().union(*(h.core for h in (anchor or model.hands))),
        mats)
    if hand_mats0:
        real_hands = []
        for h in model.hands:
            kept_chains = []
            for ch in h.chains:
                tot = on = 0
                for b in ch:
                    for m, n in mats.get(b, {}).items():
                        tot += n
                        if m in hand_mats0:
                            on += n
                if tot == 0 or on / tot < 0.5:
                    log("  chain %r rejected: %s, not a finger"
                        % (ch[0], "weapon-textured" if tot else "no skin"))
                    continue
                kept_chains.append(ch)
            if len(kept_chains) < 3:
                # nearly every 'finger' was weapon-textured: this fan is
                # a weapon structure, not a hand (kart body, dummy stack)
                log("  hand at fan %r discarded: %d real fingers left"
                    % (h.fan, len(kept_chains)))
                continue
            if len(kept_chains) != len(h.chains):
                h.chains = kept_chains
                h.core = rebuild_core(skel, h.fan, h.wrist, kept_chains)
            real_hands.append(h)
        if not real_hands:
            raise RuntimeError("no hands found in the original model "
                               "(all candidate fans were weapon parts)")
        model.hands = real_hands

    ext: set[str] = set()
    for h in model.hands:
        ext |= h.core

    def blocked(b: str, top: str) -> bool:
        """Path b -> top crosses a hand bone? Then b re-roots anyway and
        must not stop the arm climb (famas: STOCK under the wrist)."""
        p = skel.parent.get(b)
        while p is not None and p != top:
            if p in ext:
                return True
            p = skel.parent.get(p)
        return False

    for h in model.hands:
        a = skel.parent.get(h.wrist)
        while a is not None:
            outside = {b for b in skel.subtree(a)
                       if b not in ext and b != a
                       and total.get(b, 0) > 1e-3 and not blocked(b, a)}
            if outside:
                break
            ext.add(a)
            a = skel.parent.get(a)

    # sleeves: CSO-style models skin the forearm sleeve to its own bone
    # under the wrist ('Bone_Se_Hand-R'). It is not a finger chain, but
    # its TEXTURES are the hand's — absorb any subtree hanging off a hand
    # bone whose skin is >=90% hand-material (a shell prop on a fingertip
    # or a famas STOCK uses weapon textures and stays).
    hand_mats = hand_materials(model, ext, mats)
    grew = True
    while grew:
        grew = False
        for b in list(skel.names):
            if b in ext or skel.parent.get(b) not in ext:
                continue
            sub = skel.subtree(b)
            counts: dict[str, int] = {}
            for s in sub:
                for m, n in mats.get(s, {}).items():
                    counts[m] = counts.get(m, 0) + n
            total_n = sum(counts.values())
            if not total_n:
                continue
            on_hand = sum(n for m, n in counts.items() if m in hand_mats)
            if on_hand / total_n >= 0.9:
                ext |= sub
                grew = True
                log("  sleeve subtree %r absorbed into the hand (%d%% "
                    "hand-textured)" % (b, 100 * on_hand // total_n))

    log("Hand bones to remove: %d of %d" % (len(ext), len(skel.names)))
    return ext


def _bone_materials(model: WeaponModel) -> dict[str, dict[str, int]]:
    """bone name -> {material: corner count} over all reference meshes."""
    out: dict[str, dict[str, int]] = {}
    for smd in model.refs.values():
        n_of = smd.name_of()
        for tri in smd.triangles:
            for v in tri.verts:
                d = out.setdefault(n_of[v.dominant_bone()], {})
                d[tri.material] = d.get(tri.material, 0) + 1
    return out


def hand_materials(model: WeaponModel, core: set[str],
                   mats: dict[str, dict[str, int]] | None = None) -> set[str]:
    """Materials that BELONG to the hands: the majority of their corners
    sit on hand-core bones. Majority matters — a gauntlet weapon
    (balrog1) puts weapon-textured triangles ON finger bones, and that
    texture must stay a weapon material."""
    mats = mats or _bone_materials(model)
    per_mat: dict[str, list] = {}
    for bone, counts in mats.items():
        for m, n in counts.items():
            rec = per_mat.setdefault(m, [0, 0, set()])
            rec[0] += n
            if bone in core:
                rec[1] += n
                rec[2].add(bone)
    # the load-bearing signal is SPREAD: skin textures touch many finger
    # bones (knife glove: 7, hands: 17-31) while weapon textures touch
    # 0-2 core bones even when skinned to the hand (balrog gauntlet: 2,
    # knife blade riding the wrist: 1). The fraction guard is deliberately
    # low — with only one detectable hand anchoring the census, a genuine
    # hand texture can drop to ~0.32 of corners on core (cartblue).
    return {m for m, (tot, on, bones) in per_mat.items()
            if tot and on / tot > 0.25 and len(bones) >= 5}


def classify_meshes(model: WeaponModel, hand_bones: set[str],
                    hand_mats: set[str] | None = None, log=print):
    """-> (dropped mesh names, kept mesh names).

    A mesh is dropped as 'the old hands' only if BOTH its weights sit on
    hand bones AND its textures are hand textures — a gauntlet weapon is
    skinned to hand bones but weapon-textured, and must be kept."""
    dropped, kept = [], []
    for name, wmap in model.weights.items():
        total = sum(wmap.values())
        if total <= 0:
            kept.append(name)
            continue
        on_hand = sum(x for b, x in wmap.items() if b in hand_bones)
        mat_frac = 1.0
        if hand_mats is not None:
            counts: dict[str, int] = {}
            for tri in model.refs[name].triangles:
                counts[tri.material] = counts.get(tri.material, 0) + 1
            n = sum(counts.values())
            mat_frac = sum(c for m, c in counts.items()
                           if m in hand_mats) / n if n else 0.0
        if total > 0 and on_hand / total >= HAND_WEIGHT_FRACTION \
                and mat_frac >= 0.5:
            dropped.append(name)
        else:
            kept.append(name)
            if on_hand > 1e-3:
                log("  %r is mixed: %.0f%% of weights on hand bones — hand "
                    "triangles will be cut out" % (name, 100 * on_hand / total))
    log("Dropped hand meshes: %s; kept: %s" % (dropped, kept))
    return dropped, kept


def assemble(model: WeaponModel, plan: RetargetPlan,
             setup_world_old: dict[str, np.ndarray], log=print) -> Assembly:
    skel = model.skel
    hand_bones = model._hand_bones  # set by convert()
    dropped, kept_meshes = model._dropped, model._kept

    # kept weapon geometry -> which original bones must stay
    kept_dominant: set[str] = set()
    for mesh in kept_meshes:
        smd = model.refs[mesh]
        n_of = smd.name_of()
        for tri in smd.triangles:
            doms = {n_of[v.dominant_bone()] for v in tri.verts}
            if doms & hand_bones:
                continue  # cut out (mixed mesh) or re-attached later
            kept_dominant |= doms

    keep: set[str] = set()
    for b in kept_dominant:
        keep.add(b)
        for a in skel.ancestors(b):
            if a in hand_bones:
                break
            keep.add(a)
    kept_weapon = [n for n in skel.names if n in keep]
    deleted = set(skel.names) - keep

    parent_name: dict[str, str | None] = {}
    for b in kept_weapon:
        p = skel.parent[b]
        while p is not None and p not in keep:
            p = skel.parent.get(p)
        parent_name[b] = p

    cso_bones = plan.cso_bones()
    for c in cso_bones:
        parent_name[c] = plan.asset.bones[c].parent

    name_clash = set(cso_bones) & set(kept_weapon)
    if name_clash:
        raise RuntimeError("bone name clash between CSO rig and weapon: %s"
                           % sorted(name_clash))

    nodes: list[Node] = []
    id_of: dict[str, int] = {}
    for name in cso_bones + kept_weapon:
        id_of[name] = len(nodes)
        p = parent_name[name]
        nodes.append(Node(id_of[name], name,
                          id_of[p] if p is not None else -1))

    setup_world_cso = solve_frame(plan, setup_world_old)
    return Assembly(nodes=nodes, id_of=id_of, parent_name=parent_name,
                    cso_bones=cso_bones, kept_weapon=kept_weapon,
                    deleted=deleted, setup_world_cso=setup_world_cso,
                    setup_world_old=setup_world_old, dropped_meshes=dropped)


# --------------------------------------------------------------------------
# skeleton key generation


def _locals_from_worlds(asm: Assembly, world_cso, world_old):
    """One output frame: id -> (pos, rot euler) for every merged node."""
    frame = {}
    for name in asm.cso_bones:
        p = asm.parent_name[name]
        m = world_cso[name] if p is None \
            else inv_rigid(world_cso[p]) @ world_cso[name]
        rot = np.array(mat3_to_euler(orthonormalize(m[:3, :3])))
        frame[asm.id_of[name]] = (m[:3, 3].copy(), rot)
    for name in asm.kept_weapon:
        p = asm.parent_name[name]
        m = world_old[name] if p is None \
            else inv_rigid(world_old[p]) @ world_old[name]
        rot = np.array(mat3_to_euler(orthonormalize(m[:3, :3])))
        frame[asm.id_of[name]] = (m[:3, 3].copy(), rot)
    return frame


def bind_frame(asm: Assembly, model: WeaponModel) -> dict:
    """time 0 of every reference SMD: weapon bones at original bind,
    CSO bones at the setup grip."""
    world_old_bind = model.skel.bind_world
    frame = {}
    for name in asm.cso_bones:
        p = asm.parent_name[name]
        w = asm.setup_world_cso
        m = w[name] if p is None else inv_rigid(w[p]) @ w[name]
        frame[asm.id_of[name]] = (m[:3, 3].copy(),
                                  np.array(mat3_to_euler(
                                      orthonormalize(m[:3, :3]))))
    for name in asm.kept_weapon:
        p = asm.parent_name[name]
        m = world_old_bind[name] if p is None \
            else inv_rigid(world_old_bind[p]) @ world_old_bind[name]
        frame[asm.id_of[name]] = (m[:3, 3].copy(),
                                  np.array(mat3_to_euler(
                                      orthonormalize(m[:3, :3]))))
    return frame


# --------------------------------------------------------------------------
# meshes


def build_hands_reference(asm: Assembly, model: WeaponModel,
                          plan: RetargetPlan) -> Smd:
    """CSO mesh re-skinned into the setup grip, rigid per vertex."""
    asset = plan.asset
    present = set(asm.cso_bones)
    world = asm.setup_world_cso
    tris = []
    for tri in asset.triangles:
        doms = []
        for corner in tri["corners"]:
            dom = max(corner["weights"], key=lambda p: p[1])[0]
            doms.append(dom)
        if any(d not in present for d in doms):
            continue  # a side the model doesn't have
        verts = []
        for corner, dom in zip(tri["corners"], doms):
            m = world[dom] @ inv_rigid(asset.bones[dom].rest_world)
            pos = m[:3, :3] @ np.array(corner["pos"]) + m[:3, 3]
            nrm = m[:3, :3] @ np.array(corner["normal"])
            n = np.linalg.norm(nrm)
            if n > 1e-9:
                nrm = nrm / n
            u, v = corner["uv"]
            verts.append(Vertex(bone=asm.id_of[dom], pos=pos, normal=nrm,
                                uv=(u, v)))
        tris.append(Triangle(tri["mat"], verts))
    frame = bind_frame(asm, model)
    return Smd(nodes=asm.nodes, frames=[frame], triangles=tris)


def rebuild_weapon_reference(asm: Assembly, model: WeaponModel,
                             plan: RetargetPlan, mesh: str,
                             log=print) -> Smd:
    """Kept reference mesh: geometry passes through; bone ids remapped.
    Hand-dominated triangles are cut (mixed meshes); stray vertices glued
    to a deleted bone are re-attached to that side's CSO wrist so they
    keep following the hand rigidly."""
    smd = model.refs[mesh]
    n_of = smd.name_of()
    hand_bones = model._hand_bones
    hand_mats = getattr(model, "_hand_mats", None)

    # deleted-bone -> CSO bone: paired fingers map finger-to-finger so a
    # gauntlet plate riding a finger keeps riding the matching CSO finger
    anchor_map: dict[str, str] = {}
    side_of_bone: dict[str, str] = {}
    wrist_of_side = {sp.hand.side: sp for sp in plan.sides}
    for h in model.hands:
        for b in h.core:
            side_of_bone[b] = h.side
    for sp in plan.sides:
        anchor_map[sp.hand.wrist] = sp.rig.wrist
        anchor_map[sp.hand.fan] = sp.rig.wrist
        for oc, cc in sp.pairs:
            for i in range(min(len(oc), len(cc))):
                anchor_map[oc[i]] = cc[i]

    def cso_anchor(old_bone: str):
        if old_bone in anchor_map:
            return anchor_map[old_bone]
        sp = wrist_of_side.get(side_of_bone.get(old_bone))
        return sp.rig.wrist if sp else None

    tris, cut, reattached = [], 0, 0
    for tri in smd.triangles:
        doms = [n_of[v.dominant_bone()] for v in tri.verts]
        if all(d in hand_bones for d in doms) \
                and (hand_mats is None or tri.material in hand_mats):
            cut += 1
            continue
        verts = []
        ok = True
        for v, dom in zip(tri.verts, doms):
            if dom in asm.id_of:
                verts.append(Vertex(bone=asm.id_of[dom], pos=v.pos.copy(),
                                    normal=v.normal.copy(), uv=v.uv))
                continue
            anchor = cso_anchor(dom)
            if anchor is None or anchor not in asm.id_of:
                ok = False
                break
            # keep the vertex glued to where the old bone would be at the
            # setup pose, then let the CSO wrist carry it
            m = (asm.setup_world_old[dom]
                 @ inv_rigid(model.skel.bind_world[dom]))
            pos = m[:3, :3] @ v.pos + m[:3, 3]
            nrm = m[:3, :3] @ v.normal
            verts.append(Vertex(bone=asm.id_of[anchor], pos=pos,
                                normal=nrm, uv=v.uv))
            reattached += 1
        if ok:
            tris.append(Triangle(tri.material, verts))
        else:
            cut += 1
    if cut or reattached:
        log("  %r: cut %d hand triangles, re-attached %d vertices"
            % (mesh, cut, reattached))
    frame = bind_frame(asm, model)
    return Smd(nodes=asm.nodes, frames=[frame], triangles=tris)


# --------------------------------------------------------------------------
# sequences


def build_sequence(asm: Assembly, model: WeaponModel, plan: RetargetPlan,
                   anim: Smd) -> Smd:
    frames_world = anim_world_frames(anim, model.skel)
    out_frames = []
    for world_old in frames_world:
        world_cso = solve_frame(plan, world_old)
        out_frames.append(_locals_from_worlds(asm, world_cso, world_old))
    return Smd(nodes=asm.nodes, frames=out_frames, triangles=[])


# --------------------------------------------------------------------------
# survivors / QC fixes


def compute_survivors(asm: Assembly, out_refs: dict[str, Smd]) -> set[str]:
    """Bones studiomdl will keep: weighted in some reference + ancestors."""
    name_by_id = {n.id: n.name for n in asm.nodes}
    weighted: set[str] = set()
    for smd in out_refs.values():
        for tri in smd.triangles:
            for v in tri.verts:
                weighted.add(name_by_id[v.bone])
    survivors = set(weighted)
    for b in list(weighted):
        p = asm.parent_name.get(b)
        while p is not None:
            survivors.add(p)
            p = asm.parent_name.get(p)
    asm.survivors = survivors
    return survivors


def attachment_fixes(asm: Assembly, model: WeaponModel, plan,
                     log=print) -> dict[int, tuple | None]:
    fixes: dict[int, tuple | None] = {}
    skel = model.skel
    weapon_anchors = [b for b in asm.kept_weapon if b in asm.survivors]
    side_of_bone = {b: h.side for h in model.hands for b in h.core}
    cso_wrist = {sp.hand.side: sp.rig.wrist for sp in plan.sides}
    for att in model.qc.attachments:
        bone, idx = att["bone"], att["idx"]
        if bone in asm.survivors:
            continue
        if bone not in skel.bind_world:
            log("  WARNING: $attachment %d bone %r unknown — dropped"
                % (idx, bone))
            fixes[idx] = None
            continue
        # Attachment on a REMOVED HAND bone -> follow that side's CSO wrist
        # (the bone that replaces it), preserving its world position at the
        # setup grip. The wrist always survives and the attachment still
        # rides the hand, unlike an arbitrary nearby weapon helper (which may
        # itself be a hand-parented bone that studiomdl then collapses).
        side = side_of_bone.get(bone)
        if side in cso_wrist and cso_wrist[side] in asm.id_of:
            anchor = cso_wrist[side]
            world4 = asm.setup_world_old[bone] @ np.append(
                np.asarray(att["offset"], dtype=float), 1.0)
            new_off = inv_rigid(asm.setup_world_cso[anchor]) @ world4
            fixes[idx] = (anchor, tuple(float(x) for x in new_off[:3]))
            log("  $attachment %d: %r -> %r (hand wrist)" % (idx, bone, anchor))
            continue
        world = skel.bind_world[bone][:3, :3] @ np.array(att["offset"]) \
            + skel.bind_world[bone][:3, 3]
        anchor = None
        for a in skel.ancestors(bone):
            if a in asm.survivors:
                anchor = a
                break
        if anchor is None and weapon_anchors:
            anchor = min(weapon_anchors,
                         key=lambda n: np.linalg.norm(
                             skel.bind_world[n][:3, 3] - world))
        if anchor is None:
            log("  WARNING: no anchor for $attachment %d — dropped" % idx)
            fixes[idx] = None
            continue
        new_off = inv_rigid(skel.bind_world[anchor]) @ np.append(world, 1.0)
        fixes[idx] = (anchor, tuple(float(x) for x in new_off[:3]))
        log("  $attachment %d: %r -> %r (%.3f %.3f %.3f)"
            % (idx, bone, anchor, *new_off[:3]))
    return fixes
