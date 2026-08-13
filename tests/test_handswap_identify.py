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
