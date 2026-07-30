"""Tests for the ``move-weapon`` command.

Builds a model with ``replace-hands`` then slides the weapon, checking the gun
moves by exactly the world offset while the hands and animations are untouched,
and that repeated calls accumulate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from valve_qc_merger.commands.move_weapon import move_weapon
from valve_qc_merger.commands.replace_hands import replace_hands
from valve_qc_merger.kinematics import world_transforms
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.parsers.smd import parse_smd_file


def _elite() -> Path:
    return Path(__file__).resolve().parents[1] / "tmp" / "pistols" / "view" / "v_elite"


def _hands() -> Path:
    return Path(__file__).resolve().parents[1] / "tmp" / "hands"


requires_elite = pytest.mark.skipif(
    not (_elite().exists() and _hands().exists()),
    reason="v_elite sample not available",
)


def _first_vertex_world(path: Path) -> Vector3:
    smd = parse_smd_file(path)
    world = world_transforms(smd.nodes, smd.frames[0])
    v = smd.triangles[0].vertices[0]
    return world[v.bone].transform_point(v.position)


@requires_elite
def test_move_weapon_slides_the_gun_by_the_world_offset(tmp_path: Path) -> None:
    build = replace_hands(_elite(), _hands(), tmp_path / "out").output_dir
    gun, hand = build / "v_elite-PV.smd", build / "grafted_male.smd"
    anim = next((build / "v_elite_anims").glob("*.smd"))
    gun_before = _first_vertex_world(gun)
    hand_before = _first_vertex_world(hand)
    anim_bytes = anim.read_bytes()

    result = move_weapon(build, Vector3(1.0, -2.0, 0.5))
    assert result.output_dir == build and result.studios_moved >= 1

    gun_after = _first_vertex_world(gun)
    assert abs(gun_after.x - gun_before.x - 1.0) < 1e-3
    assert abs(gun_after.y - gun_before.y + 2.0) < 1e-3
    assert abs(gun_after.z - gun_before.z - 0.5) < 1e-3

    hand_after = _first_vertex_world(hand)  # hands do not move
    assert abs(hand_after.x - hand_before.x) < 1e-6
    assert abs(hand_after.y - hand_before.y) < 1e-6
    assert abs(hand_after.z - hand_before.z) < 1e-6
    assert anim.read_bytes() == anim_bytes  # animations untouched


@requires_elite
def test_move_weapon_is_cumulative(tmp_path: Path) -> None:
    build = replace_hands(_elite(), _hands(), tmp_path / "out").output_dir
    gun = build / "v_elite-PV.smd"
    start = _first_vertex_world(gun)
    move_weapon(build, Vector3(0.0, 0.0, 1.0))
    move_weapon(build, Vector3(0.0, 0.0, 1.0))
    end = _first_vertex_world(gun)
    assert abs(end.z - start.z - 2.0) < 1e-3


@requires_elite
def test_move_weapon_to_output_leaves_the_build_untouched(tmp_path: Path) -> None:
    build = replace_hands(_elite(), _hands(), tmp_path / "out").output_dir
    before = _first_vertex_world(build / "v_elite-PV.smd")
    result = move_weapon(build, Vector3(3.0, 0.0, 0.0), tmp_path / "moved")

    assert result.output_dir == tmp_path / "moved"
    assert abs(_first_vertex_world(build / "v_elite-PV.smd").x - before.x) < 1e-9
    moved = _first_vertex_world(tmp_path / "moved" / "v_elite-PV.smd")
    assert abs(moved.x - before.x - 3.0) < 1e-3


def _blender_available() -> bool:
    from valve_qc_merger.commands.clear_weapon import _find_blender

    try:
        _find_blender()
    except Exception:
        return False
    return True


requires_blender = pytest.mark.skipif(not _blender_available(), reason="Blender not installed")


@requires_elite
@requires_blender
def test_clear_weapon_slides_the_gun_out_of_the_grip(tmp_path: Path) -> None:
    from valve_qc_merger.commands.clear_weapon import clear_weapon

    build = replace_hands(_elite(), _hands(), tmp_path / "out").output_dir
    before = _first_vertex_world(build / "v_elite-PV.smd")
    result = clear_weapon(build)

    assert result.overlap_after < result.overlap_before  # grip clipping reduced
    after = _first_vertex_world(build / "v_elite-PV.smd")
    moved = (after.x - before.x) ** 2 + (after.y - before.y) ** 2 + (after.z - before.z) ** 2
    assert moved > 0.01  # the gun actually moved
