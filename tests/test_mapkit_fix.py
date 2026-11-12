"""mapkit stage 2: traces, generated spawns, the fix plan and ``map fix``."""

from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np

from tests.mapkit_fixtures import build_map, room_solids
from valve_qc_merger.cli import main
from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.analyze import check_map
from valve_qc_merger.mapkit.fix import FixOptions, FixPlan, next_name, plan_fixes, save_plan
from valve_qc_merger.mapkit.hull import (
    EMPTY,
    SOLID,
    STAND,
    point_contents,
    segments_clear,
    trace_hull,
)
from valve_qc_merger.mapkit.rules import Rules
from valve_qc_merger.mapkit.spawns import CT, SpawnOptions, T, fix_stuck, generate
from valve_qc_merger.server.entities import parse

WORLD = {"classname": "worldspawn"}
SEALED = ((-8.0, -256.0, 0.0), (8.0, 256.0, 256.0))  # a wall right across: two rooms


def _spawn(cls: str, x: float, y: float, z: float = 37.0) -> dict[str, str]:
    return {"classname": cls, "origin": f"{x:g} {y:g} {z:g}", "angles": "0 0 0"}


def _map(tmp_path: Path, entities: list[dict[str, str]], walls: list | None = None) -> Path:
    maps = tmp_path / "game" / "maps"
    maps.mkdir(parents=True, exist_ok=True)
    solids, outer = room_solids(walls=walls)
    path = maps / "zm_test.bsp"
    path.write_bytes(build_map(entities, solids=solids, outer=outer, external=[]))
    return path


# -- traces ---------------------------------------------------------------------

def test_trace_hull_stops_at_the_wall() -> None:
    bsp = B.Bsp.parse(build_map([WORLD]))
    trace = trace_hull(bsp, [-200, 0, 40], [200, 0, 40])
    assert abs(trace.end[0] - (-24.0)) < 0.1  # the wall at -8, a player 16 wide
    assert trace.normal.tolist() == [-1.0, 0.0, 0.0] and not trace.start_solid
    down = trace_hull(bsp, [-200, 0, 100], [-200, 0, -100])
    assert abs(down.end[2] - 36.0) < 0.1 and down.normal[2] == 1.0
    free = trace_hull(bsp, [-200, 0, 40], [-100, 0, 40])
    assert free.fraction == 1.0 and free.end.tolist() == [-100.0, 0.0, 40.0]
    assert trace_hull(bsp, [0, 0, 40], [100, 0, 40]).start_solid


def test_segments_clear() -> None:
    bsp = B.Bsp.parse(build_map([WORLD]))
    clear = segments_clear(bsp, [[-200, 0, 40], [-200, 200, 40]], [[200, 0, 40], [200, 200, 40]])
    assert clear.tolist() == [False, True]  # through the wall / through the doorway


# -- spawns ---------------------------------------------------------------------

def test_generated_spawns_stand_free_apart_and_only_where_players_get(tmp_path: Path) -> None:
    entities = [WORLD, _spawn(CT, -400, -200), _spawn(T, -400, 200)]
    bsp = B.Bsp.read(_map(tmp_path, entities, [SEALED]))
    options = SpawnOptions(per_team=6, grid=48, min_distance=64)
    result = generate(bsp, entities, options)
    assert len(result.added) == 10  # 5 more a team
    assert sum(p.classname == CT for p in result.added) == 5
    points = np.array([p.origin for p in result.added])
    assert (point_contents(bsp, points + [0, 0, 1], STAND) == EMPTY).all()
    assert (point_contents(bsp, points - [0, 0, 3], STAND) == SOLID).all()  # on the floor
    assert (points[:, 0] < -8).all()  # never in the closed room behind the wall
    everyone = np.vstack([points, [[-400, -200, 37], [-400, 200, 37]]])
    for a, b in itertools.combinations(everyone, 2):
        assert np.linalg.norm(a - b) >= 64
    assert generate(bsp, entities, options).added == result.added  # the same every time


def test_without_spawns_the_largest_region_is_used(tmp_path: Path) -> None:
    bsp = B.Bsp.read(_map(tmp_path, [WORLD]))
    result = generate(bsp, [WORLD], SpawnOptions(per_team=3))
    assert len(result.added) == 6 and result.reachable > 0


def test_a_stuck_spawn_is_moved_out_of_the_wall() -> None:
    bsp = B.Bsp.parse(build_map([WORLD]))
    moved = fix_stuck(bsp, (-14.0, 0.0, 37.0))
    assert moved is not None and moved[0] <= -24
    assert point_contents(bsp, [(moved[0], moved[1], moved[2] + 1)], STAND)[0] == EMPTY
    assert fix_stuck(bsp, (0.0, 0.0, 37.0), reach=8.0) is None  # deep inside: removed


# -- the plan -------------------------------------------------------------------

def test_plan_operations_apply_to_a_copy() -> None:
    entities = [WORLD, {"classname": "light"}, {"classname": "particle_emitter",
                                                "model": "*1", "rendermode": "5"}]
    plan = FixPlan(Path("x.bsp"))
    plan.add("entity.remove", "", 1)
    plan.add("cso.replace", "", 2, classname="func_illusionary", extra={})
    plan.add("entity.add", "", None, new={"classname": "info_target", "targetname": "m"})
    plan.add("worldspawn.wad", "", None, value="a.wad;")
    plan.add("entity.remove", "worldspawn is never removed", 0)
    out = plan.apply(entities)
    assert [e["classname"] for e in out] == ["worldspawn", "func_illusionary", "info_target"]
    assert out[1]["model"] == "*1" and out[1]["rendermode"] == "5"
    assert out[0]["wad"] == "a.wad;" and "wad" not in entities[0]
    assert plan.crc_safe and not plan.needs_new_name


def test_map_fix_keeps_the_crc_and_backs_up(tmp_path: Path) -> None:
    entities = [WORLD, _spawn(CT, -400, -200), _spawn(T, -400, 200), _spawn(CT, -14, -200),
                {"classname": "particle_emitter", "origin": "0 0 64"},
                {"classname": "cycler_sprite", "model": "models/gone.mdl", "origin": "1 1 1"}]
    path = _map(tmp_path, entities)
    crc = B.Bsp.read(path).crc()
    rules = Rules(min_spawns=6, max_players=12)
    plan = plan_fixes(check_map(path, rules=rules), FixOptions(markers=[("zp_supply", 3)]),
                      rules)
    codes = {op.code for op in plan.active}
    assert {"cso.remove", "entity.remove", "spawn.fix", "spawn.generate", "entity.add"} <= codes
    result = save_plan(plan)
    assert not result.new_name and result.crc_after == f"{crc:08x}"
    assert path.with_name("zm_test.bsp.bak").is_file()
    after = check_map(path, rules=rules)
    assert after.spawns["ct"] == 6 and after.spawns["t"] == 6 and after.spawns["bad"] == 0
    assert not after.of_level("critical")
    fixed = parse(B.Bsp.read(path).entity_text)
    assert sum(e.get("targetname") == "zp_supply" for e in fixed) == 3
    assert not any(e.get("classname") == "particle_emitter" for e in fixed)


def test_an_ent_file_leaves_the_bsp_alone(tmp_path: Path) -> None:
    path = _map(tmp_path, [WORLD, _spawn(CT, -400, 0)])
    before = path.read_bytes()
    plan = plan_fixes(check_map(path), FixOptions(spawns=3))
    save_plan(plan, ent=True)
    assert path.read_bytes() == before
    ent = parse(path.with_suffix(".ent").read_text(encoding="latin-1"))
    assert sum(e.get("classname") == CT for e in ent) == 3


def test_a_change_players_must_see_gets_a_new_name(tmp_path: Path) -> None:
    path = _map(tmp_path, [WORLD])
    game = path.parent.parent
    (game / "maps" / "zm_test.txt").write_text("a map")
    (game / "overviews").mkdir()
    (game / "overviews" / "zm_test.txt").write_text("IMAGE overviews/zm_test.bmp")
    (game / "mapcycle.txt").write_text("de_dust2\nzm_test\n")
    (game / "maps" / "zm_test_sp.bsp").write_bytes(b"taken")
    plan = FixPlan(path)
    plan.add("worldspawn.wad", "a WAD with new textures", None, client_visible=True,
             value="new.wad;")
    result = save_plan(plan, mapcycle=game / "mapcycle.txt")
    assert result.new_name and result.map_name == "zm_test_sp2"
    assert (game / "maps" / "zm_test_sp2.bsp").is_file()
    assert (game / "maps" / "zm_test_sp2.txt").read_text() == "a map"
    assert (game / "overviews" / "zm_test_sp2.txt").is_file()
    assert (game / "mapcycle.txt").read_text() == "de_dust2\nzm_test_sp2\n"
    assert path.read_bytes() == _map(tmp_path / "again", [WORLD]).read_bytes()  # untouched


def test_next_name() -> None:
    maps = Path("/nonexistent")
    assert next_name(maps, "zm_forbidden") == "zm_forbidden_sp"
    assert next_name(maps, "zm_forbidden_sp") == "zm_forbidden_sp2"
    assert next_name(maps, "zm_forbidden_sp2") == "zm_forbidden_sp3"


def test_map_fix_command(tmp_path: Path) -> None:
    path = _map(tmp_path, [WORLD, _spawn(CT, -400, 0), _spawn(T, 400, 0)])
    before = path.read_bytes()
    assert main(["map", "fix", str(path), "--spawns", "4", "--dry-run"]) == 0
    assert path.read_bytes() == before
    out = tmp_path / "out"
    # 4 a team is still too few for the zp profile's 32 players: the check after says so
    assert main(["map", "fix", str(path), "--spawns", "4", "--out", str(out)]) == 2
    assert path.read_bytes() == before  # --out: the original stays as it was
    fixed = parse(B.Bsp.read(out / "maps" / "zm_test.bsp").entity_text)
    assert sum(e.get("classname") in (CT, T) for e in fixed) == 8
