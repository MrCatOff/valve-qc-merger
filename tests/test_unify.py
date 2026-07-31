"""Gun-subtree discovery and arm->gun assignment tests (§7.7), no Blender."""

from __future__ import annotations

import pytest

from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.retarget.correspondence import BoneMap, Correspondence, RigBone
from valve_qc_merger.retarget.unify import (
    UnifyError,
    assign_wrists,
    discover_guns,
)


def _bone(name: str, parent: str | None, x: float = 0.0) -> RigBone:
    return RigBone(name, parent, Vector3(x, 0.0, 0.0), Vector3(x, 1.0, 0.0))


# Two arms, each: root -> forearm -> wrist(+1 finger); the gun hangs off the
# forearm (as on v_elite), gun root -> gun tip.
_SRC = [
    _bone("armL", None, 0.0),
    _bone("foreL", "armL", 0.0),
    _bone("wristL", "foreL", 0.0),
    _bone("fingerL", "wristL", 0.0),
    _bone("gunL", "foreL", 0.5),
    _bone("gunL_tip", "gunL", 0.5),
    _bone("armR", None, 10.0),
    _bone("foreR", "armR", 10.0),
    _bone("wristR", "foreR", 10.0),
    _bone("fingerR", "wristR", 10.0),
    _bone("gunR", "foreR", 10.5),
    _bone("gunR_tip", "gunR", 10.5),
]
_HAND = {"armL", "foreL", "wristL", "fingerL", "armR", "foreR", "wristR", "fingerR"}
_WEAPON = {"gunL", "gunL_tip", "gunR", "gunR_tip"}


def _corr() -> Correspondence:
    return Correspondence(
        maps=[
            BoneMap("Bip01 L Hand", "wristL", "wrist", "L"),
            BoneMap("Bip01 R Hand", "wristR", "wrist", "R"),
        ],
        score=2.0, margin=4.0,
    )


def test_discover_guns_finds_each_subtree() -> None:
    guns = discover_guns(_SRC, _HAND, _WEAPON)
    roots = {g.root for g in guns}
    assert roots == {"gunL", "gunR"}
    by_root = {g.root: g for g in guns}
    assert set(by_root["gunL"].bones) == {"gunL", "gunL_tip"}
    assert by_root["gunL"].src_parent == "foreL"


def test_discover_guns_ignores_non_weapon_boundary() -> None:
    # A non-hand child that carries no weapon weight is not a gun.
    src = [*_SRC, _bone("strapL", "foreL", 0.0)]
    guns = discover_guns(src, _HAND, _WEAPON)
    assert {g.root for g in guns} == {"gunL", "gunR"}


def test_assign_wrists_matches_gun_to_its_arm() -> None:
    guns = discover_guns(_SRC, _HAND, _WEAPON)
    assignment = assign_wrists(guns, _SRC, _corr())
    assert assignment == {"gunL": "Bip01 L Hand", "gunR": "Bip01 R Hand"}


def test_assign_wrists_rejects_non_bijection() -> None:
    # Both guns hang off the same arm -> two guns want one wrist: not a bijection.
    src = [b for b in _SRC if b.name not in {"gunR", "gunR_tip"}]
    src = [*src, _bone("gunR", "foreL", 0.5), _bone("gunR_tip", "gunR", 0.5)]
    guns = discover_guns(src, _HAND, _WEAPON)
    with pytest.raises(UnifyError, match="bijection"):
        assign_wrists(guns, src, _corr())
