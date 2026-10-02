"""handswap hand identification + mesh classification on CSO-style rigs.

The stock 29 viewmodels have one single-studio hands mesh per hand; the CSO
Nexon catalogue breaks three assumptions the stock corpus never exercised:
  * male/female hand MESHES share one L/R name, so both wrists' meshes hint
    the same side (fixed by disambiguating on the wrist BONE name);
  * a dedicated hand mesh also skins forearm / secondary-hand helper bones,
    dragging its strict-hand fraction well below the drop threshold (fixed by
    measuring a QC-labelled hand mesh against the hand+arm region);
  * a weapon whose own bone tree fans out like fingers registers as a false
    hand skinned only by the weapon mesh (fixed by rejecting it when a real,
    QC-labelled hand survives).
"""

from __future__ import annotations

from types import SimpleNamespace

from valve_qc_merger.handswap import build as buildmod
from valve_qc_merger.handswap.identify import HandInfo, _resolve_sides


def _model(weights, refs):
    return SimpleNamespace(weights=weights,
                           qc=SimpleNamespace(references=refs))


def _hand(wrist, meshes):
    return HandInfo(side="?", wrist=wrist, fan=wrist, chains=[], tip_dirs={},
                    core=set(), source_meshes=set(meshes))


# --- side disambiguation --------------------------------------------------- #

def test_sides_split_by_wrist_bone_when_meshes_share_side() -> None:
    # both hands' meshes say "_L_" (the male/female mesh is shared) — only the
    # wrist BONE name tells them apart; both must NOT collapse to one side
    hl = _hand("Bone_Lefthand", {"CSO_Hand_Male_L_2009"})
    hr = _hand("Bone_Righthand", {"CSO_Hand_Male_L_2009"})
    _resolve_sides(None, [hl, hr], {}, log=lambda *a: None)
    assert (hl.side, hr.side) == ("left", "right")


def test_sides_split_by_mesh_name_stock_style() -> None:
    hr = _hand("BoneA", {"rhand"})
    hl = _hand("BoneB", {"lhand"})
    _resolve_sides(None, [hr, hl], {}, log=lambda *a: None)
    assert (hr.side, hl.side) == ("right", "left")


# --- mesh classification --------------------------------------------------- #

def test_hand_mesh_with_forearm_weight_dropped() -> None:
    # A dedicated CSO hands mesh also skins forearm / secondary-hand helper
    # bones; hand_bone_set covers that whole hand+arm region, so the mesh sits
    # entirely on hand bones (>= 0.95) and is dropped. (No hand_mats given, so
    # the material census is skipped and classification is weight-only.)
    hand_bones = {"Wrist", "F0", "Forearm", "SeHand"}
    weights = {
        "gun": {"Gun0": 10.0, "Gun1": 5.0},
        "cso_hands": {"Wrist": 3.0, "F0": 1.0, "Forearm": 4.0, "SeHand": 2.0},
    }
    refs = [("weapon", "gun"), ("hands", "cso_hands")]
    dropped, kept = buildmod.classify_meshes(_model(weights, refs), hand_bones,
                                             log=lambda *a: None)
    assert dropped == ["cso_hands"]
    assert kept == ["gun"]


def test_unlabeled_weapon_mesh_kept() -> None:
    hand_bones = {"Wrist"}
    weights = {"gun": {"Gun0": 10.0, "Wrist": 0.1}}
    refs = [("weapon", "gun")]
    dropped, kept = buildmod.classify_meshes(_model(weights, refs), hand_bones,
                                             log=lambda *a: None)
    assert dropped == [] and kept == ["gun"]


def test_pure_hand_mesh_dropped_by_strict_rule_without_label() -> None:
    # no /hand/ bodygroup label -> the strict >=0.95 rule still catches a pure
    # hand mesh (stock behaviour)
    hand_bones = {"Wrist", "F0"}
    weights = {"rhand": {"Wrist": 5.0, "F0": 5.0}, "gun": {"Gun": 10.0}}
    refs = [("body", "gun"), ("body", "rhand")]
    dropped, kept = buildmod.classify_meshes(_model(weights, refs), hand_bones,
                                             log=lambda *a: None)
    assert dropped == ["rhand"] and kept == ["gun"]


# --- a weapon rig hanging under a wrist ------------------------------------ #

def _skeleton(bones):
    """bones: [(name, parent, (x, y, z) local offset)] -> OrigSkeleton."""
    import numpy as np

    from valve_qc_merger.handswap.identify import OrigSkeleton
    names = [b[0] for b in bones]
    parent = {b[0]: b[1] for b in bones}
    children = {n: [] for n in names}
    for name, par, _ in bones:
        if par is not None:
            children[par].append(name)
    skel = OrigSkeleton(names=names, parent=parent, children=children,
                        bind_local={b[0]: (np.array(b[2], float), np.zeros(3))
                                    for b in bones},
                        bind_world={})
    skel.bind_world = skel.world_at({})
    return skel


def _fingers(wrist, prefix, spread=1.0):
    out = []
    for i in range(5):
        out.append((f"{prefix}{i}a", wrist, (2.0, (i - 2) * spread, 0.0)))
        out.append((f"{prefix}{i}b", f"{prefix}{i}a", (1.0, 0.0, 0.0)))
    return out


def test_weapon_fan_under_a_named_wrist_does_not_evict_the_hand() -> None:
    # v_janus1: the gun's rig hangs under 'Bone_Lefthand' and its body fans
    # out into five part-chains. The deepest-only fan rule used to keep the
    # gun fan, drop the real left hand above it, then drop the gun fan as a
    # weapon-mesh-only "hand": the left hand vanished from the retarget.
    from valve_qc_merger.handswap.identify import find_hands
    bones = [("Root", None, (0, 0, 0)),
             ("Bone_Lefthand", "Root", (0, 10, 0)),
             ("Bone_Righthand", "Root", (0, -10, 0)),
             ("gun_root", "Bone_Lefthand", (1, 0, -2)),
             ("gun_body", "gun_root", (3, 0, 0))]
    bones += _fingers("Bone_Lefthand", "L") + _fingers("Bone_Righthand", "R")
    bones += _fingers("gun_body", "G", spread=1.5)
    skel = _skeleton(bones)
    hand_bones = [b[0] for b in bones if b[0][0] in "LR" and b[0][1].isdigit()]
    weights = {
        "hands": {**{b: 10.0 for b in hand_bones},
                  "Bone_Lefthand": 30.0, "Bone_Righthand": 30.0},
        "gun": {b[0]: 40.0 for b in bones if b[0].startswith(("G", "gun"))},
    }
    hands = find_hands(skel, {}, weights, hand_labeled={"hands"}, log=lambda *a: None)
    assert sorted(h.wrist for h in hands) == ["Bone_Lefthand", "Bone_Righthand"]
    assert {h.wrist: h.side for h in hands} == {"Bone_Lefthand": "left",
                                                 "Bone_Righthand": "right"}
