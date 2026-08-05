"""Gun-subtree discovery and arm->gun assignment tests (§7.7), no Blender."""

from __future__ import annotations

from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.retarget.correspondence import BoneMap, Correspondence, RigBone
from valve_qc_merger.retarget.unify import (
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


# A grafted template rig (glock18): the gun is a SEPARATE root tree, disjoint
# from the hands, whose root carries no weapon weight (a pure pivot) but whose
# subtree does. Its root is placed near the left wrist (x≈0).
_SEP_SRC = [
    _bone("armL", None, 0.0),
    _bone("foreL", "armL", 0.0),
    _bone("wristL", "foreL", 0.0),
    _bone("fingerL", "wristL", 0.0),
    _bone("armR", None, 10.0),
    _bone("foreR", "armR", 10.0),
    _bone("wristR", "foreR", 10.0),
    _bone("fingerR", "wristR", 10.0),
    _bone("USP", None, 1.0),          # separate weapon root (unweighted pivot)
    _bone("gunBody", "USP", 1.0),     # weapon geometry
]
_SEP_HAND = {"armL", "foreL", "wristL", "fingerL", "armR", "foreR", "wristR", "fingerR"}
_SEP_WEAPON = {"gunBody"}


def test_discover_guns_finds_separate_root() -> None:
    guns = discover_guns(_SEP_SRC, _SEP_HAND, _SEP_WEAPON)
    assert {g.root for g in guns} == {"USP"}
    gun = guns[0]
    assert gun.src_parent is None              # a disjoint root, not off a hand
    assert set(gun.bones) == {"USP", "gunBody"}


def test_assign_wrists_attaches_separate_root_to_nearest_wrist() -> None:
    guns = discover_guns(_SEP_SRC, _SEP_HAND, _SEP_WEAPON)
    # USP sits at x=1, so the left wrist (x=0) is nearer than the right (x=10).
    assert assign_wrists(guns, _SEP_SRC, _corr()) == {"USP": "Bip01 L Hand"}


def test_assign_wrists_allows_multiple_guns_per_arm() -> None:
    # A multi-part weapon: two weapon roots both hang off the LEFT arm (gun body
    # + a loose shell/prop). Both attach to that arm's wrist — no bijection is
    # enforced, since the reach rule already sends each to its own arm.
    src = [b for b in _SRC if b.name not in {"gunR", "gunR_tip"}]
    src = [*src, _bone("gunR", "foreL", 0.5), _bone("gunR_tip", "gunR", 0.5)]
    guns = discover_guns(src, _HAND, _WEAPON)
    assignment = assign_wrists(guns, src, _corr())
    assert assignment == {"gunL": "Bip01 L Hand", "gunR": "Bip01 L Hand"}
