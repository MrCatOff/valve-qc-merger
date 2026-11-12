"""mapkit: the rest of the spec — texture area and prefixes, animated
replacement, preview marks, import renames and sky shrinking, bspguy and
subdivided recompiles."""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

from tests.mapkit_fixtures import Face, build_map, floor_faces, texture
from valve_qc_merger.cli import main
from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit import external as X
from valve_qc_merger.mapkit.fix import (
    animation_frames,
    frame_images,
    plan_replace_textures,
    save_plan,
)
from valve_qc_merger.mapkit.nexon import SourceIndex, import_map
from valve_qc_merger.mapkit.render import OLD_COLOUR, render_plan
from valve_qc_merger.mapkit.textures import extract, texture_uses
from valve_qc_merger.preview.render import encode_png
from valve_qc_merger.server.entities import parse
from valve_qc_merger.sprite.wad import read_wad, write_wad

posix = pytest.mark.skipif(os.name == "nt", reason="fake tools are POSIX scripts")


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "cache"))
    monkeypatch.setattr(X, "config_dir", lambda: tmp_path / "config")


def _png(path: Path, colour: tuple[int, int, int]) -> Path:
    rgba = np.zeros((16, 16, 4), np.uint8)
    rgba[..., :3], rgba[..., 3] = colour, 255
    path.write_bytes(encode_png(rgba))
    return path


def test_texture_area_and_prefixes() -> None:
    bsp = B.Bsp.parse(build_map(embedded=[texture("{grate", (1, 1, 1)),
                                          texture("+0lava", (2, 2, 2))], external=[]))
    uses = {u.name: u for u in texture_uses(bsp, {})}
    assert uses["{grate"].area == pytest.approx(512 * 512)  # one half of the 1024 x 512 floor
    assert uses["{grate"].prefixes == ["transparent"]
    assert uses["+0lava"].prefixes == ["animated"]


def test_an_animation_is_replaced_frame_by_frame(tmp_path: Path) -> None:
    game = tmp_path / "cstrike"
    (game / "maps").mkdir(parents=True)
    frames = [texture(f"+{k}fire", (10, 10, 10)) for k in range(3)]
    faces = floor_faces() + [Face([(-8, -8, 1), (8, -8, 1), (8, 8, 1), (-8, 8, 1)], 2)]
    path = game / "maps" / "zm_anim.bsp"
    path.write_bytes(build_map(embedded=frames, external=[], faces=faces))
    assert animation_frames(path, "fire") == ["+0fire", "+1fire", "+2fire"]
    pictures = [_png(tmp_path / f"fire_{k}.png", (80 * k + 40, 0, 0)) for k in range(3)]
    assert frame_images(pictures[0]) == pictures
    plan = plan_replace_textures(path, [("+0fire", pictures[0])])
    assert [op.data["name"] for op in plan.active] == ["+0fire", "+1fire", "+2fire"]
    result = save_plan(plan, res=False)
    reds = {t.name: t.rgba()[0, 0, 0] for t in extract(B.Bsp.read(
        path.with_name(f"{result.map_name}.bsp")))}
    assert reds["+0fire"] < reds["+1fire"] < reds["+2fire"]


def test_external_replacements_share_one_wad_copy(tmp_path: Path) -> None:
    game = tmp_path / "cstrike"
    (game / "maps").mkdir(parents=True)
    world = {"classname": "worldspawn", "wad": "ext.wad"}
    path = game / "maps" / "zm_two.bsp"
    path.write_bytes(build_map([world], embedded=[], external=[("a_tex", 16, 16),
                                                               ("b_tex", 16, 16)]))
    (game / "ext.wad").write_bytes(write_wad([texture("a_tex", (1, 1, 1)),
                                              texture("b_tex", (1, 1, 1))]))
    plan = plan_replace_textures(path, [("a_tex", _png(tmp_path / "a.png", (200, 0, 0))),
                                        ("b_tex", _png(tmp_path / "b.png", (0, 200, 0)))])
    save_plan(plan, res=False)
    copy = {t.name: t.rgba()[0, 0, :3].tolist() for t in read_wad(game / "ext_zm_two.wad")}
    assert copy["a_tex"][0] > 150 and copy["b_tex"][1] > 150  # both in the one copy


def test_the_plan_marks_issues_and_greys_old_spawns() -> None:
    entities = [{"classname": "worldspawn"},
                {"classname": "info_player_start", "origin": "-300 0 37"},
                {"classname": "info_player_start", "origin": "300 0 37"},
                {"classname": "cycler_sprite", "origin": "0 -100 0"}]
    plan = render_plan(B.Bsp.parse(build_map(entities)), size=256, marks=[3],
                       grey_spawns_before=2)
    old = plan.to_pixel(-300, 0)
    assert tuple(plan.image[int(old[1]), int(old[0]), :3]) == OLD_COLOUR
    mark = plan.to_pixel(0, -100)
    assert tuple(plan.image[int(mark[1]), int(mark[0]), :3]) == (255, 210, 40)
    assert plan.floor_at(-300, 0) == pytest.approx(0.0)


def _tga(width: int, height: int, colour: tuple[int, int, int]) -> bytes:
    return (struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, width, height, 24, 0x20)
            + bytes([colour[2], colour[1], colour[0]]) * (width * height))


def test_import_renames_a_wad_and_a_sky_the_base_game_has_otherwise(tmp_path: Path) -> None:
    cs = tmp_path / "client" / "cstrike"
    (cs / "maps").mkdir(parents=True)
    world = {"classname": "worldspawn", "wad": "\\cso\\halflife.wad", "skyname": "desert"}
    (cs / "maps" / "zm_r.bsp").write_bytes(build_map([world], external=[("wall_ext", 16, 16)]))
    (cs / "halflife.wad").write_bytes(write_wad([texture("wall_ext", (9, 9, 9))]))
    (cs / "gfx" / "env").mkdir(parents=True)
    for side in ("up", "dn", "lf", "rt", "ft", "bk"):
        (cs / "gfx" / "env" / f"desert{side}.tga").write_bytes(_tga(512, 512, (0, 0, 200)))
    base = tmp_path / "steam" / "valve"
    (base / "gfx" / "env").mkdir(parents=True)
    (base / "halflife.wad").write_bytes(write_wad([texture("other", (1, 1, 1))]))
    (base / "gfx" / "env" / "desertup.tga").write_bytes(_tga(4, 4, (200, 0, 0)))
    out = tmp_path / "server" / "cstrike"
    result = import_map("zm_r", SourceIndex.build([tmp_path / "client"]), out, base=[base],
                        shrink_sky=True)
    assert (out / "halflife_cso.wad").is_file()
    assert (out / "gfx" / "env" / "desert_csoup.tga").is_file()
    head = (out / "gfx" / "env" / "desert_csoup.tga").read_bytes()[:18]
    assert struct.unpack_from("<HH", head, 12) == (256, 256)
    world_after = parse(B.Bsp.read(out / "maps" / "zm_r.bsp").entity_text)[0]
    assert world_after["skyname"] == "desert_cso" and "halflife_cso.wad" in world_after["wad"]
    assert any("sky side 512x512 -> at most 256" in c for f in result.files.values()
               for c in f.conversions)


def _script(folder: Path, name: str, body: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(f"#!{sys.executable}\nimport sys, shutil\n{body}\n")
    path.chmod(0o755)
    return path


@posix
def test_bspguy_runs_on_a_copy_and_installs_a_new_map(tmp_path: Path,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    game = tmp_path / "game"
    (game / "maps").mkdir(parents=True)
    path = game / "maps" / "zm_g.bsp"
    path.write_bytes(build_map())
    usage = "usage: bspguy <command> <map>\\n  noclip  remove clipnodes\\n  merge  merge maps"
    guy = _script(tmp_path / "bin", "bspguy",
                  f"if len(sys.argv) < 3: print('{usage}'); sys.exit(0)\nprint('done')")
    X.save_settings({"bspguy": {"path": str(guy)}})
    assert X.bspguy_commands(X.find_tool("bspguy")) == ["merge", "noclip"]
    assert main(["map", "bspguy", str(path)]) == 0
    assert "merge, noclip" in capsys.readouterr().out
    assert main(["map", "bspguy", str(path), "simplify"]) == 3
    assert main(["map", "bspguy", str(path), "noclip"]) == 2  # 0 spawns: critical, but saved
    assert (game / "maps" / "zm_g_sp.bsp").is_file()


@posix
def test_bad_extents_are_recompiled_with_subdivide(tmp_path: Path) -> None:
    game = tmp_path / "game"
    (game / "maps").mkdir(parents=True)
    path = game / "maps" / "zm_e.bsp"
    path.write_bytes(build_map(external=[]))
    log = tmp_path / "args.txt"
    settings: dict[str, dict[str, object]] = {
        "decompiler": {"path": str(_script(tmp_path / "bin", "dec",
                                           "open(sys.argv[2], 'w').write('{\\n}\\n')"))}}
    for step in X.COMPILERS:
        body = (f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                "if '--help' in sys.argv: print('  -subdivide #  cut faces'); sys.exit(0)")
        if step == "hlrad":
            body += f"\nshutil.copy({str(path)!r}, sys.argv[-1] + '.bsp')"
        settings[step] = {"path": str(_script(tmp_path / "bin", step, body))}
    result = X.recompile_subdivided(path, 200, settings=settings)
    assert result.bsp is not None
    assert "-subdivide 200 zm_e" in log.read_text()
