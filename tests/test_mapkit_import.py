"""mapkit stage 5: importing maps from unpacked CSO folders, ``map build``."""

from __future__ import annotations

import json
import shutil
import struct
from pathlib import Path

import numpy as np
import pytest

from tests.mapkit_fixtures import build_map, texture
from valve_qc_merger.cli import main
from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.analyze import check_map
from valve_qc_merger.mapkit.fix import FixOptions, plan_fixes, save_plan
from valve_qc_merger.mapkit.nexon import ImportResult, SourceIndex, import_map, write_record
from valve_qc_merger.mapkit.rules import Rules
from valve_qc_merger.mapkit.workflow import BuildOptions, build_maps
from valve_qc_merger.server.entities import parse
from valve_qc_merger.sound.wav import read_wav, write_wav
from valve_qc_merger.sprite.wad import write_wad

EXAMPLES = Path(__file__).parent / "examples" / "mdl"


@pytest.fixture(autouse=True)
def _cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "cache"))


def _spawn(cls: str, x: float, y: float) -> dict[str, str]:
    return {"classname": cls, "origin": f"{x:g} {y:g} 37", "angles": "0 0 0"}


def _client(tmp_path: Path) -> Path:
    """An unpacked CSO client: cstrike/ with a map, its WAD in a subfolder,
    a v9 model in mixed case, a stereo sound, a sky; valve/ too."""
    client = tmp_path / "client"
    cs = client / "cstrike"
    (cs / "maps").mkdir(parents=True)
    entities = [{"classname": "worldspawn", "wad": "\\cso\\data\\cstrike\\ZM_Test.wad",
                 "skyname": "nite"},
                _spawn("info_player_start", -400, 0), _spawn("info_player_deathmatch", 400, 0),
                {"classname": "cycler_sprite", "model": "models/Props/Mini.mdl",
                 "origin": "0 200 0"},
                {"classname": "cycler_sprite", "model": "models/shared.mdl", "origin": "0 0 0"},
                {"classname": "ambient_generic", "message": "ambience/Wind.wav"},
                {"classname": "cycler_sprite", "model": "models/same.mdl", "origin": "9 9 0"}]
    (cs / "maps" / "zm_test.bsp").write_bytes(build_map(entities))
    (cs / "wads").mkdir()
    (cs / "wads" / "ZM_Test.wad").write_bytes(
        write_wad([texture("wall_ext", (90, 90, 200), 64)]))
    (cs / "models" / "Props").mkdir(parents=True)
    data = bytearray((EXAMPLES / "mini.mdl").read_bytes())
    data[4:8] = struct.pack("<i", 9)  # CSO's version 9: version 10's layout
    (cs / "models" / "Props" / "Mini.mdl").write_bytes(bytes(data))
    (cs / "models" / "shared.mdl").write_bytes((EXAMPLES / "mini.mdl").read_bytes() + b"cso")
    (cs / "models" / "same.mdl").write_bytes((EXAMPLES / "mini.mdl").read_bytes())
    (cs / "sound" / "ambience").mkdir(parents=True)
    stereo = np.column_stack([np.sin(np.arange(2205) / 9), np.cos(np.arange(2205) / 9)]) * 0.4
    (cs / "sound" / "ambience" / "Wind.wav").write_bytes(write_wav(stereo, 22050))
    (cs / "gfx" / "env").mkdir(parents=True)
    for side in ("up", "dn", "lf", "rt", "ft", "bk"):
        (cs / "gfx" / "env" / f"nite{side}.tga").write_bytes(
            struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, 2, 2, 24, 0) + bytes(12))
    (client / "valve").mkdir()
    return client


def _base(tmp_path: Path) -> Path:
    """A clean game: cstrike/ with a stock model of the same name (other
    content) and one identical to the client's."""
    base = tmp_path / "steam" / "cstrike"
    (base / "models").mkdir(parents=True)
    (base / "models" / "shared.mdl").write_bytes((EXAMPLES / "mini.mdl").read_bytes())
    (base / "models" / "same.mdl").write_bytes((EXAMPLES / "mini.mdl").read_bytes())
    return base


def test_the_source_index_finds_files_in_any_case(tmp_path: Path) -> None:
    client = _client(tmp_path)
    index = SourceIndex.build([client])
    assert index.find("MODELS/props/mini.MDL") == client / "cstrike" / "models" / "Props" / \
        "Mini.mdl"
    assert index.find_named("zm_test.wad", ".wad") is not None
    assert list((tmp_path / "cache" / "valve-qc-merger" / "source-index").glob("*.json"))
    assert SourceIndex.build([client]).files == index.files  # from the cache


def test_import_takes_the_closure_and_fixes_names(tmp_path: Path) -> None:
    client, base = _client(tmp_path), _base(tmp_path)
    out = tmp_path / "server" / "cstrike"
    result = import_map("zm_test", SourceIndex.build([client]), out, base=[base])
    write_record(result, out)
    assert (out / "ZM_Test.wad").is_file()  # WADs keep the map's spelling
    model = out / "models" / "props" / "mini.mdl"
    assert model.is_file() and struct.unpack_from("<i", model.read_bytes(), 4)[0] == 10
    assert "studio version 9 -> 10" in result.files["models/props/mini.mdl"].conversions
    wav = read_wav(out / "sound" / "ambience" / "wind.wav")
    assert wav.channels == 1 and wav.bits == 16
    assert all((out / "gfx" / "env" / f"nite{s}.tga").is_file()
               for s in ("up", "dn", "lf", "rt", "ft", "bk"))
    assert result.files["models/same.mdl"].state == "stock"
    assert not (out / "models" / "same.mdl").exists()
    assert result.files["models/shared.mdl"].state == "renamed"
    assert (out / "models" / "shared_cso.mdl").is_file()
    entities = parse(B.Bsp.read(out / "maps" / "zm_test.bsp").entity_text)
    models = {e.get("model") for e in entities}
    assert {"models/props/mini.mdl", "models/shared_cso.mdl", "models/same.mdl"} <= models
    assert any(e.get("message") == "ambience/wind.wav" for e in entities)
    original = B.Bsp.read(client / "cstrike" / "maps" / "zm_test.bsp")
    assert B.Bsp.read(out / "maps" / "zm_test.bsp").crc() == original.crc()
    record = json.loads((out / "import.json").read_text())
    assert record["maps"] == ["zm_test"] and "models/props/mini.mdl" in record["files"]
    report = check_map(out / "maps" / "zm_test.bsp", [base])
    assert not [i for i in report.issues if i.code in ("missing_model", "missing_wad",
                                                       "missing_texture", "case_mismatch")]


def test_a_file_the_game_folder_has_is_kept(tmp_path: Path) -> None:
    client = _client(tmp_path)
    out = tmp_path / "server" / "cstrike"
    (out / "models" / "props").mkdir(parents=True)
    (out / "models" / "props" / "mini.mdl").write_bytes(b"mine")
    result = import_map("zm_test", SourceIndex.build([client]), out, result=ImportResult())
    assert result.files["models/props/mini.mdl"].state == "kept"
    assert (out / "models" / "props" / "mini.mdl").read_bytes() == b"mine"
    import_map("zm_test", SourceIndex.build([client]), out, overwrite=True)
    assert (out / "models" / "props" / "mini.mdl").read_bytes()[:4] == b"IDST"


def test_map_fix_copies_missing_files_from_a_source(tmp_path: Path) -> None:
    game = tmp_path / "game"
    (game / "maps").mkdir(parents=True)
    path = game / "maps" / "zm_x.bsp"
    path.write_bytes(build_map([{"classname": "worldspawn"},
                                {"classname": "cycler_sprite", "model": "models/a/Mini.mdl"}]))
    source = tmp_path / "dump" / "Models" / "A"
    source.mkdir(parents=True)
    shutil.copy(EXAMPLES / "mini.mdl", source / "mini.mdl")
    plan = plan_fixes(check_map(path), FixOptions(spawns=0, sources=[tmp_path / "dump"]))
    assert [op.code for op in plan.active] == ["file.copy"]
    save_plan(plan)
    assert not [i for i in check_map(path).issues if i.code == "missing_model"]


def test_build_runs_the_whole_way(tmp_path: Path) -> None:
    client, base = _client(tmp_path), _base(tmp_path)
    out = tmp_path / "server" / "cstrike"
    rules = Rules(min_spawns=4, max_players=8)
    rules.extra["markers"] = ["zp_supply:2"]
    results = build_maps(["zm_test"], rules, BuildOptions(out=out, sources=[client],
                                                          folders=[base], props=False))
    build = results[0]
    assert not build.error, build.trace
    assert build.after is not None and not build.after.of_level("critical")
    assert build.after.spawns["ct"] == 4 and build.after.spawns["t"] == 4
    entities = parse(B.Bsp.read(out / "maps" / "zm_test.bsp").entity_text)
    assert sum(e.get("targetname") == "zp_supply" for e in entities) == 2
    assert (out / "import.json").is_file() and (out / "maps" / "zm_test.res").is_file()


def test_map_import_and_build_commands(tmp_path: Path) -> None:
    client, base = _client(tmp_path), _base(tmp_path)
    out = tmp_path / "server" / "cstrike"
    code = main(["map", "import", "zm_test", "--source", str(client), "--out", str(out),
                 "--game", str(base)])
    assert code == 2  # imported; 1 + 1 spawns are critical for the zp profile
    assert main(["map", "import", "nothere", "--source", str(client), "--out", str(out)]) == 3
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"profiles": {"tiny": {"min_spawns": 3, "max_players": 6,
                                                       "props": False}}}))
    report = tmp_path / "build.json"
    code = main(["map", "build", str(out / "maps" / "zm_test.bsp"), "--rules", str(rules),
                 "--profile", "tiny", "--game", str(base), "--json", str(report)])
    assert code in (0, 1)
    assert json.loads(report.read_text())[0]["after"]["spawns"]["ct"] == 3
