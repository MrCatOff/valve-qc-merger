"""Foreign-hands assets and the round-trip benchmark."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from valve_qc_merger.handswap import asset as assetmod
from valve_qc_merger.handswap.bench import (
    compare,
    is_native,
    native_hand_meshes,
    run_weapon,
    write_report,
)
from valve_qc_merger.handswap.foreign import asset_from_weapon, write_asset

_DONOR = Path("tests/examples/pair_deagle/v_deagle")
_ANACONDA = Path("tests/examples/v_anaconda")


@pytest.fixture(scope="module")
def native(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """v_anaconda with its male hand mesh named like the CSO 2009 set."""
    target = tmp_path_factory.mktemp("native") / "v_anaconda"
    shutil.copytree(_ANACONDA, target)
    (target / "grafted_male.smd").rename(target / "CSO_Hand_Male_L_2009.smd")
    qc = target / "v_anaconda.qc"
    qc.write_text(qc.read_text(encoding="latin-1").replace(
        '"grafted_male"', '"CSO_Hand_Male_L_2009"'), encoding="latin-1")
    return target


@pytest.fixture(scope="module")
def foreign(tmp_path_factory: pytest.TempPathFactory) -> str:
    return write_asset(asset_from_weapon(str(_DONOR)),
                       str(tmp_path_factory.mktemp("asset") / "valve.json.gz"))


def test_foreign_asset_is_a_full_rig(foreign: str) -> None:
    hands = assetmod.load(foreign)
    for side, suffix in (("right", ".R"), ("left", ".L")):
        rig = hands.sides[side]
        assert rig.arm == [f"UpperArm{suffix}", f"Arm0{suffix}", f"Arm1{suffix}"]
        assert [c[0] for c in rig.chains] == [f"{f}00{suffix}" for f in assetmod.FINGERS]
    # +Y runs along every bone, frames are rigid
    for bone in hands.bones.values():
        r = bone.rest_world[:3, :3]
        assert np.allclose(r.T @ r, np.eye(3), atol=1e-6)
    assert len(hands.triangles) > 500


def test_native_detection(native: Path) -> None:
    assert is_native(native) and native_hand_meshes(native) == ["CSO_Hand_Male_L_2009"]
    assert not is_native(_DONOR)


def test_round_trip_measures_an_error(native: Path, foreign: str, tmp_path: Path) -> None:
    identity, trip = run_weapon(native, foreign, tmp_path)
    assert not identity.error and not trip.error, (identity.error, trip.error)
    for result in (identity, trip):
        assert np.isfinite(result.mean) and result.mean >= 0
        assert result.sequences and all(s.frames > 0 for s in result.sequences)
    # going through smaller foreign hands loses more than the direct retarget
    assert trip.mean >= identity.mean
    # the comparison is deterministic (re-measuring gives the same number)
    assert compare(native, tmp_path / "identity" / native.name, "identity").mean == \
        pytest.approx(identity.mean)
    write_report([identity, trip], tmp_path / "report")
    table = (tmp_path / "report.md").read_text()
    assert "v_anaconda" in table and "Round trip over 1 weapons" in table


def test_skin_centroids_on_both_hands(native: Path) -> None:
    """The finger fit compares skin with skin: our asset knows where each
    segment's flesh sits (rest frame), the original hands at the grip."""
    from valve_qc_merger.handswap import build as buildmod
    hands = assetmod.load()
    for rig in hands.sides.values():
        for chain in rig.chains:
            assert chain[-1] in hands.centroids_local
    model = buildmod.load_weapon(str(native), None, log=lambda *a: None)
    setup = buildmod.anim_world_frames(model.anims[model.qc.sequences[0]["smd"]],
                                       model.skel)[0]
    centroids = buildmod.skin_centroids(model, ["CSO_Hand_Male_L_2009"], setup)
    for hand in model.hands:
        for chain in hand.chains:
            assert chain[-1] in centroids
            # a fingertip's flesh sits near its bone, not at the origin
            assert np.linalg.norm(centroids[chain[-1]] - setup[chain[-1]][:3, 3]) < 3.0


def test_grip_offset_never_changes_the_finger_pairing() -> None:
    """A palm offset moves the chosen seat; it must not steer the pairing
    search (an old v_deagle tuning offset picked the wrong thumb)."""
    from valve_qc_merger.handswap import build as buildmod
    from valve_qc_merger.handswap.retarget import build_plan
    model = buildmod.load_weapon(str(_DONOR), None, log=lambda *a: None)
    setup = buildmod.anim_world_frames(model.anims[model.qc.sequences[0]["smd"]],
                                       model.skel)[0]
    hands = assetmod.load()
    plain = build_plan(hands, model.skel, model.hands, setup, log=lambda *a: None)
    moved = build_plan(hands, model.skel, model.hands, setup, log=lambda *a: None,
                       grip_offsets={"left": [0.0, 0.0, -0.6], "right": [0.3, 0.0, 0.0]})
    for a, b in zip(plain.sides, moved.sides, strict=True):
        assert a.pairs == b.pairs
        shift = np.linalg.norm(a.desired_setup[:3, 3] - b.desired_setup[:3, 3])
        assert 0.25 < shift < 0.7  # the offset itself still applies


def test_wrist_kink_is_kept_in_the_native_range() -> None:
    from valve_qc_merger.handswap.retarget import WRIST_NATIVE, _natural_arm, _palm_angles
    palm = np.eye(4)  # X fingers, Y across, Z normal
    rest = (0.0, 0.0)
    for side, ((p_med, p_lo, p_hi), (y_med, y_lo, y_hi)) in WRIST_NATIVE.items():
        # an arm bent 70 deg up and 50 deg sideways comes back into range
        steep = -np.array([1.0, 0.0, 0.0]) + np.tan(np.radians(50)) * np.array([0, 1.0, 0]) \
            + np.tan(np.radians(70)) * np.array([0, 0, 1.0])
        out = _natural_arm(palm, steep / np.linalg.norm(steep), rest, side)
        pitch, yaw = _palm_angles(palm, out)
        assert p_lo - 1e-6 <= pitch <= p_hi + 1e-6 and y_lo - 1e-6 <= yaw <= y_hi + 1e-6
        # a wrist already at the native median stays where it is
        med = -np.array([1.0, 0, 0]) + np.tan(np.radians(y_med)) * np.array([0, 1.0, 0]) \
            + np.tan(np.radians(p_med)) * np.array([0, 0, 1.0])
        med /= np.linalg.norm(med)
        assert np.allclose(_natural_arm(palm, med, rest, side), med, atol=1e-9)
