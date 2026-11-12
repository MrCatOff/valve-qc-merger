"""mapkit: the BSP core, hulls, the map check, the plan and the ``map`` command."""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import numpy as np
import pytest

from tests.mapkit_fixtures import Face, build_map, floor_faces, texture
from valve_qc_merger.cli import main
from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.analyze import check_map
from valve_qc_merger.mapkit.geometry import geometry
from valve_qc_merger.mapkit.hull import CROUCH, EMPTY, SOLID, STAND, point_contents
from valve_qc_merger.mapkit.render import CT_COLOUR, T_COLOUR, render_plan
from valve_qc_merger.mapkit.rules import Rules
from valve_qc_merger.mapkit.textures import broken_animations
from valve_qc_merger.server.entities import entity_text, parse, with_entities
from valve_qc_merger.sprite.wad import write_wad

WORLD = {"classname": "worldspawn", "wad": "\\cso\\data\\cstrike\\ext.wad", "skyname": ""}


def _spawn(cls: str, x: float, y: float, z: float = 37.0, yaw: float = 0.0) -> dict[str, str]:
    return {"classname": cls, "origin": f"{x:g} {y:g} {z:g}", "angles": f"0 {yaw:g} 0"}


def _game(tmp_path: Path, entities: list[dict[str, str]], **kw: object) -> Path:
    """A game folder with maps/test.bsp and ext.wad (holding wall_ext)."""
    game = tmp_path / "cstrike"
    (game / "maps").mkdir(parents=True)
    (game / "maps" / "test.bsp").write_bytes(build_map(entities, **kw))  # type: ignore[arg-type]
    (game / "ext.wad").write_bytes(write_wad([texture("wall_ext", (90, 90, 200), 64)]))
    return game / "maps" / "test.bsp"


# -- the BSP core ---------------------------------------------------------------

def test_an_unchanged_map_writes_back_byte_for_byte() -> None:
    data = build_map([WORLD])
    bsp = B.Bsp.parse(data)
    assert bsp.write() == data
    assert bsp.changed() == []


def test_an_entity_edit_keeps_the_crc_and_every_other_lump() -> None:
    data = build_map([WORLD, _spawn("info_player_start", -200, 0)])
    bsp = B.Bsp.parse(data)
    crc = bsp.crc()
    entities = parse(bsp.entity_text) + [_spawn("info_player_deathmatch", 200, 0)] * 30
    bsp.set_entity_text(entity_text(entities))
    again = B.Bsp.parse(bsp.write())
    assert again.crc() == crc
    assert len(parse(again.entity_text)) == 32
    assert all(again.lumps[i] == B.Bsp.parse(data).lumps[i] for i in range(1, B.LUMPS))
    # the old writer agrees
    assert B.Bsp.parse(with_entities(data, entities)).crc() == crc


def test_a_lump_of_a_new_size_moves_the_ones_after_it_aligned() -> None:
    data = build_map([WORLD])
    bsp = B.Bsp.parse(data)
    bsp.set_lump(B.TEXTURES, bsp.lumps[B.TEXTURES] + b"\x01\x02\x03")
    out = bsp.write()
    table = [struct.unpack_from("<2i", out, 4 + i * 8) for i in range(B.LUMPS)]
    assert all(offset % 4 == 0 for offset, _n in table)
    again = B.Bsp.parse(out)
    original = B.Bsp.parse(data)
    assert [i for i in range(B.LUMPS) if again.lumps[i] != original.lumps[i]] == [B.TEXTURES]
    assert again.crc() != original.crc()
    # lumps before the changed one stay where they were
    assert table[B.ENTITIES][0] == original.offsets[B.ENTITIES]


def test_blue_shift_maps_are_told_and_written_standard() -> None:
    data = bytearray(build_map([WORLD]))
    first, second = data[4:12], data[12:20]
    data[4:12], data[12:20] = second, first
    bsp = B.Bsp.parse(bytes(data))
    assert bsp.blue_shift and bsp.entity_text.startswith("{")
    fixed = B.Bsp.parse(bsp.write())
    assert not fixed.blue_shift and fixed.lumps == bsp.lumps


def test_not_a_v30_map_is_refused() -> None:
    with pytest.raises(B.BspError, match="version 29"):
        B.Bsp.parse(struct.pack("<i", 29) + bytes(B.HEADER))


# -- hulls and geometry ---------------------------------------------------------

def test_points_against_the_hulls() -> None:
    bsp = B.Bsp.parse(build_map([WORLD]))
    points = [(-200, 0, 40), (0, 0, 40), (12, 0, 40), (30, 0, 40), (-200, 0, 20), (0, 200, 40),
              (-200, 0, 250), (2000, 0, 40)]
    assert point_contents(bsp, points, 0).tolist() == [EMPTY, SOLID, EMPTY, EMPTY, EMPTY,
                                                       EMPTY, EMPTY, SOLID]
    # a standing player is 16 wide, 36 down and up: near walls, floor and ceiling it does not fit
    assert point_contents(bsp, points, STAND).tolist() == [EMPTY, SOLID, SOLID, EMPTY, SOLID,
                                                           EMPTY, SOLID, SOLID]
    # crouching (18 down) it fits 20 units over the floor
    assert point_contents(bsp, [(-200, 0, 20)], CROUCH).tolist() == [EMPTY]


def test_surface_extents_as_the_engine_rounds_them() -> None:
    faces = floor_faces() + [Face([(-512, -256, 1), (-512, 256, 1), (512, 256, 1),
                                   (512, -256, 1)][::-1], 0, 1.0),
                             Face([(-512, -256, 2), (512, -256, 2), (512, 256, 2),
                                   (-512, 256, 2)], 0, 1.0, special=True)]
    geo = geometry(B.Bsp.parse(build_map([WORLD], faces=faces)))
    assert geo.surface_extents()[0].tolist() == [128, 128]  # 512 units at scale 4
    assert geo.bad_extent_faces().tolist() == [2]  # 1024 texels lit; the special one is fine


def test_broken_texture_animations() -> None:
    assert broken_animations(["+0lava", "+1lava", "+2lava"]) == []
    assert broken_animations(["+1fire", "+2fire"]) == ["+0fire"]
    assert broken_animations(["+0sign", "+asign", "+csign"]) == ["+asign"]


# -- the check ------------------------------------------------------------------

def _codes(report, level: str | None = None) -> list[str]:  # noqa: ANN001
    return [i.code for i in report.issues if level is None or i.level == level]


def test_a_clean_map_has_no_issues(tmp_path: Path) -> None:
    spawns = [_spawn("info_player_start", -400 + 40 * k, -100) for k in range(4)]
    spawns += [_spawn("info_player_deathmatch", 100 + 40 * k, -100) for k in range(4)]
    path = _game(tmp_path, [WORLD, *spawns])
    report = check_map(path, rules=Rules(min_spawns=4, max_players=8))
    assert report.level == "ok", [(i.code, i.text) for i in report.issues]
    assert report.spawns == {"ct": 4, "t": 4, "bad": 0}
    assert report.slots["total"] == 1 and report.slots["map"] == 1
    assert report.counts["faces"] == 2 and report.crc


def test_what_breaks_a_map_on_a_server(tmp_path: Path) -> None:
    entities = [
        dict(WORLD, wad=WORLD["wad"] + ";\\cso\\gone.wad"),
        _spawn("info_player_start", -200, 0),
        _spawn("info_player_start", 0, 0),  # inside the wall
        _spawn("info_player_deathmatch", 200, 0),
        {"classname": "cycler_sprite", "model": "models/forklift.mdl", "origin": "100 100 0"},
        {"classname": "cycler_sprite", "model": "models/D_tree.mdl", "origin": "-100 100 0"},
        {"classname": "particle_emitter", "origin": "0 0 64"},
        {"classname": "ambient_generic", "message": "ambience/wind.wav", "origin": "0 0 64"},
    ]
    path = _game(tmp_path, entities,
                 external=[("wall_ext", 64, 64), ("lost_tex", 64, 64)],
                 embedded=[texture("floor_a", (200, 60, 40)),
                           texture("big_one", (40, 160, 60), 512)], extra_models=3)
    (path.parent.parent / "models").mkdir()
    (path.parent.parent / "models" / "d_tree.mdl").write_bytes(b"IDST" + struct.pack("<i", 9))
    report = check_map(path, rules=Rules(min_spawns=8, max_players=8))
    critical, warning = _codes(report, "critical"), _codes(report, "warning")
    assert {"missing_model", "missing_wad", "missing_texture", "model_version",
            "few_spawns"} <= set(critical)
    assert {"spawn_in_wall", "unknown_class", "missing_sound", "case_mismatch",
            "texture_size"} <= set(warning)
    missing = next(i for i in report.issues if i.code == "missing_model")
    assert missing.where["file"] == "models/forklift.mdl" and missing.fix == "entity.remove"
    stuck = next(i for i in report.issues if i.code == "spawn_in_wall")
    assert stuck.where["entities"] == [2]
    assert report.slots["brush"] == 3 and report.slots["total"] == 1 + 3 + 2
    assert report.spawns == {"ct": 2, "t": 1, "bad": 1}
    assert report.level == "critical"
    json.dumps(report.to_dict())  # the report is plain JSON


def test_files_are_found_in_the_game_folders_given(tmp_path: Path) -> None:
    entities = [WORLD, {"classname": "cycler_sprite", "model": "models/prop.mdl"}]
    path = _game(tmp_path, entities)
    stock = tmp_path / "steam" / "valve" / "models"
    stock.mkdir(parents=True)
    (stock / "prop.mdl").write_bytes(b"IDST" + struct.pack("<i", 10))
    (tmp_path / "steam" / "cstrike").mkdir()
    assert "missing_model" in _codes(check_map(path))
    # valve next to the cstrike given is searched too
    assert "missing_model" not in _codes(check_map(path, [tmp_path / "steam" / "cstrike"]))


def test_rules_from_the_example_file() -> None:
    example = Path(__file__).resolve().parent.parent / "configs" / "mapkit.json"
    assert Rules.load(example).min_spawns == 48
    small = Rules.load(example, "small")
    assert (small.min_spawns, small.max_players, small.edicts) == (10, 20, 900 + 15 * 19)


# -- the plan -------------------------------------------------------------------

def test_the_plan_draws_floors_and_spawns(tmp_path: Path) -> None:
    entities = [WORLD, _spawn("info_player_start", -300, 0), _spawn("info_player_deathmatch",
                                                                     300, 0, yaw=90)]
    bsp = B.Bsp.parse(build_map(entities))
    plan = render_plan(bsp, size=256)
    assert max(plan.image.shape[:2]) == 256
    cx, cy = plan.to_pixel(-300, 0)
    assert tuple(plan.image[int(cy), int(cx), :3]) == CT_COLOUR
    tx, ty = plan.to_pixel(300, 0)
    assert tuple(plan.image[int(ty), int(tx), :3]) == T_COLOUR
    # the floors in their textures' colours: red half on the left, green on the right
    left = plan.image[int(cy) + 30, int(plan.to_pixel(-150, 0)[0]), :3].astype(int)
    right = plan.image[int(cy) + 30, int(plan.to_pixel(150, 0)[0]), :3].astype(int)
    assert left[0] > left[1] and right[1] > right[0]
    x, y = plan.to_world(*plan.to_pixel(123.0, -45.0))
    assert abs(x - 123) < 1e-6 and abs(y + 45) < 1e-6
    assert plan.save(tmp_path / "p.png").read_bytes()[:4] == b"\x89PNG"


def test_a_height_slice_keeps_only_its_floors() -> None:
    faces = floor_faces() + [Face([(-100, -100, 128), (100, -100, 128), (100, 100, 128),
                                   (-100, 100, 128)], 2)]
    bsp = B.Bsp.parse(build_map([WORLD], faces=faces))
    full = render_plan(bsp, size=200, spawns=False, grid=False)
    low = render_plan(bsp, size=200, spawns=False, grid=False, zmax=64)
    centre = full.to_pixel(0, 0)
    assert not np.array_equal(full.image[int(centre[1]), int(centre[0])],
                              low.image[int(centre[1]), int(centre[0])])


# -- the command ----------------------------------------------------------------

def test_map_check_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _game(tmp_path, [WORLD, {"classname": "cycler_sprite",
                                    "model": "models/gone.mdl"}])
    out = tmp_path / "report.json"
    assert main(["map", "check", str(path), "--json", str(out)]) == 2
    report = json.loads(out.read_text())
    assert report["map"] == "test" and report["version"] == 30
    assert {"bounds", "counts", "slots", "spawns", "classes", "issues"} <= set(report)
    assert "missing_model" in capsys.readouterr().out
    (tmp_path / "bad.bsp").write_bytes(b"nope")
    assert main(["map", "check", str(tmp_path / "bad.bsp")]) == 3


def test_map_preview_command(tmp_path: Path) -> None:
    path = _game(tmp_path, [WORLD, _spawn("info_player_start", 0, 200)])
    out = tmp_path / "plan.png"
    assert main(["map", "preview", str(path), "--out", str(out), "--size", "300"]) == 0
    assert out.read_bytes()[:4] == b"\x89PNG"


# -- the CSO map pack (local only) -------------------------------------------------

PACK = os.environ.get("VQM_MAP_PACK")


@pytest.mark.mappack
@pytest.mark.skipif(not PACK, reason="VQM_MAP_PACK not set (the CSO ZM map pack folder)")
def test_the_cso_pack_reports_what_was_found_by_hand() -> None:
    maps = Path(PACK or "") / "maps"
    want = {"zm_abyss": (39, set()), "zm_abyss2": (4, {"few_spawns"}),
            "zm_abyss3": (24, set()), "zm_forbidden": (39, set()),
            "zm_origin": (17, {"few_spawns"}), "zm_requiem": (36, {"missing_model"}),
            "zm_rest": (36, set()), "zm_shoreline": (7, {"few_spawns", "missing_model"})}
    for name, (spawns, critical) in want.items():
        report = check_map(maps / f"{name}.bsp")
        assert report.spawns["ct"] + report.spawns["t"] == spawns, name
        assert critical <= set(_codes(report, "critical")), name
        B.Bsp.read(maps / f"{name}.bsp").write()  # round trip
    forbidden = check_map(maps / "zm_forbidden.bsp")
    assert "unknown_class" in _codes(forbidden, "warning")
    abyss3 = check_map(maps / "zm_abyss3.bsp")
    assert sum(i.code == "texture_size" for i in abyss3.issues) == 2
    shoreline = check_map(maps / "zm_shoreline.bsp")
    assert "world_bounds" in _codes(shoreline, "warning")
