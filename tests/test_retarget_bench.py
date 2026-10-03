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
