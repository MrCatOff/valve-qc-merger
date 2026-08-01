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

import shutil
import subprocess
from pathlib import Path

import pytest

from valve_qc_merger.merge_view.discovery import load_model
from valve_qc_merger.merge_view.hands import (
    hand_bone_names,
    load_reference_rig,
    match_hands,
)
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget.config import RetargetConfig
from valve_qc_merger.retarget.driver import DriverError, find_blender
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
        idle, to_src["Bip01 R Hand"],
        [to_src[f"Bip01 R Finger{i}"] for i in (1, 2, 3, 4)],
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


def _blender_available() -> bool:
    try:
        find_blender(RetargetConfig())
        return True
    except DriverError:
        return False


@pytest.mark.skipif(not _blender_available(), reason="Blender not installed")
def test_retarget_reproduces_gold_placement(tmp_path: Path) -> None:
    """End-to-end oracle: retargeting v_deagle's idle onto the reference hands
    must land the weapon where the author-converted v_g_deagle put it."""
    weapon_dir = tmp_path / "v_deagle"
    shutil.copytree(_PAIR / "v_deagle", weapon_dir)
    out = tmp_path / "out"
    proc = subprocess.run(
        ["python", "-m", "valve_qc_merger", "retarget",
         "--weapon-dir", str(weapon_dir), "--out", str(out),
         "--sequences", "idle1"],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "hand scale: 0.857x" in proc.stdout

    out_idle = parse_smd_file(out / "anims" / "idle1.smd")
    weapon = parse_smd_file(out / "ref_deonly.smd")
    got = grip_measure(
        out_idle, "Bip01 R Hand",
        [f"Bip01 R Finger{i}" for i in (1, 2, 3, 4)],
        dominant_weapon_bone(weapon),
    )
    # Calibrated run measured +3.461 vs gold +3.462; allow solver drift.
    assert abs(got.along_palm - _GOLD_ALONG_PALM) < 0.15
