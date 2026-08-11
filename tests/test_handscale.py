"""Hand-scale measurement and the v_deagle/v_g_deagle ground-truth oracle.

The pair in ``tests/examples/pair_deagle`` shares one weapon mesh (identical
to 0.01u) but different hands: ``v_deagle`` has the old-style 0.854x rig,
``v_g_deagle`` the author-converted correctly-proportioned hands. The gold
model therefore records EXACTLY where the weapon must sit relative to a
correct hand, which calibrates the retarget's auto hand offset
(``hand_center_fraction = 1.0``, fingertip alignment).

The end-to-end oracle (needs Blender) retargets v_deagle's idle and asserts
the output reproduces the gold weapon-to-wrist placement.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from valve_qc_merger.merge_view.discovery import load_model
from valve_qc_merger.merge_view.hands import (
    hand_bone_names,
    load_reference_rig,
    match_hands,
)
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget.handscale import (
    GripMeasure,
    dominant_weapon_bone,
    grip_measure,
    measure_hand_scale,
)

_PAIR = Path("tests/examples/pair_deagle")
_REFERENCE = Path("storage/hands/reference_hands.smd")

# Ground truth measured from v_g_deagle's idle (weapon offset from the wrist,
# projected on the wrist->knuckle-centroid axis).
_GOLD_ALONG_PALM = 3.462


def _grip_of_source(model_dir: Path, weapon_stem_hint: str) -> GripMeasure:
    reference = load_reference_rig(_REFERENCE)
    model = load_model(model_dir, require_anims=False)
    fullest = max(model.meshes.values(), key=lambda m: len(m.nodes))
    match = match_hands(fullest, reference,
                        hand_bone_names(model.meshes, model.bodygroups))
    to_src = {new: old for old, new in match.renames.items()}
    weapon_smd = next(s for stem, s in model.meshes.items()
                      if weapon_stem_hint in stem)
    idle = model.anims.get("idle1") or next(iter(model.anims.values()))
    return grip_measure(
        idle, to_src["ValveBiped.Bip01_R_Hand"],
        [to_src[f"ValveBiped.Bip01_R_Finger{i}"] for i in (1, 2, 3, 4)],
        dominant_weapon_bone(weapon_smd),
    )


def test_old_hands_measure_smaller_than_reference() -> None:
    reference = load_reference_rig(_REFERENCE)
    ref_smd = parse_smd_file(_REFERENCE)
    old = parse_smd_file(_PAIR / "v_deagle" / "f_dea_Male_hand_Low.smd")
    scale = measure_hand_scale(old, reference, ref_smd)
    assert scale.chains == 8
    assert 0.84 < scale.ratio < 0.87
    assert 0.6 < scale.surplus < 0.9  # ~0.76u shorter chains than reference


def test_reference_measures_itself_at_unity() -> None:
    reference = load_reference_rig(_REFERENCE)
    ref_smd = parse_smd_file(_REFERENCE)
    scale = measure_hand_scale(ref_smd, reference, ref_smd)
    assert scale.chains == 8
    assert abs(scale.ratio - 1.0) < 1e-6
    assert abs(scale.surplus) < 1e-6


def test_gold_pair_records_the_placement_gap() -> None:
    """The pair's ground truth: correct hands hold the weapon ~1.4u further
    along the palm axis than the old small hands."""
    old = _grip_of_source(_PAIR / "v_deagle", "deonly")
    gold = _grip_of_source(_PAIR / "v_g_deagle", "Deagle_Gold")
    assert abs(old.along_palm - 2.040) < 0.05
    assert abs(gold.along_palm - _GOLD_ALONG_PALM) < 0.05
    assert 1.2 < gold.along_palm - old.along_palm < 1.6


def test_retarget_reproduces_grip(tmp_path: Path) -> None:
    """End-to-end handswap oracle (no Blender): swapping v_deagle's original
    hands for the CSO hands reproduces the authored grip with ~zero drift, the
    weapon meshes keep their exact trajectories, and the model verifies."""
    from valve_qc_merger.handswap import convert as convertmod

    out = tmp_path / "out"
    info = convertmod.convert(SimpleNamespace(
        weapon_dir=str(_PAIR / "v_deagle"), out=str(out), qc=None,
        asset=convertmod.assetmod.DEFAULT_ASSET, hands_texture=None,
        modelname=None, studiomdl=convertmod.DEFAULT_STUDIOMDL,
        compile=False, verify=True,
        snug=False, snug_max_deg=18.0, curl=[], grip_offset=[],
    ))

    report = info["verify"]
    assert report["ok"], report["errors"]
    metrics = report["metrics"]
    # the CSO wrist tracks the original wrist rigidly on every frame (the grip
    # is a constant offset from the source wrist — it can never drift)
    assert metrics["grip_pos_err"] < 0.05
    assert metrics["grip_rot_err_deg"] < 0.5
    # kept weapon bones reproduce the ORIGINAL world-space trajectories exactly
    assert metrics["weapon_traj_pos_err"] < 0.05
    # hand bones are rotation-only (Valve viewmodel shape) and keep their lengths
    assert metrics["hand_pos_channel_range"] < 1e-2
    assert metrics["hand_bone_len_dev"] < 1e-2
    # the deagle is two-handed: both hands were identified and retargeted
    assert {s["side"] for s in info["sides"]} == {"left", "right"}
