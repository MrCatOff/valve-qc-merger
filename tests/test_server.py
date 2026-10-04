"""Server budget and doctor (ReHLDS + ReGameDLL + ReAPI)."""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

from valve_qc_merger.server.bsp import MapResources, read_map_resources
from valve_qc_merger.server.budget import ProjectLoad, budget
from valve_qc_merger.server.doctor import check_folder, summary
from valve_qc_merger.server.limits import MAX_GENERIC, MAX_MODELS, level
from valve_qc_merger.server.scan import model_refs

_MINI = Path(__file__).parent / "examples" / "mdl" / "mini.mdl"


def write_bsp(path: Path, entity_text: str, brush_models: int) -> Path:
    """A minimal BSP v30: header, entity lump, models lump (64 bytes each)."""
    ents = entity_text.encode("latin-1") + b"\0"
    models = b"\0" * (64 * brush_models)
    header_size = 4 + 15 * 8
    lumps = [(0, 0)] * 15
    lumps[0] = (header_size, len(ents))
    lumps[14] = (header_size + len(ents), len(models))
    header = struct.pack("<i", 30) + b"".join(struct.pack("<2i", *lump) for lump in lumps)
    path.write_bytes(header + ents + models)
    return path


ENTITIES = """{
"classname" "worldspawn"
"wad" "\\half-life\\valve\\halflife.wad"
}
{
"classname" "ambient_generic"
"message" "ambience/wind.wav"
}
{
"classname" "env_sprite"
"model" "sprites/glow01.spr"
}
{
"classname" "cycler"
"model" "models/props/barrel.mdl"
}
{
"classname" "func_door"
"model" "*1"
"noise1" "3"
}
{
"classname" "ambient_generic"
"message" "!HG_ALERT"
}
"""


def test_map_resources_count_brush_models_and_entity_files(tmp_path: Path) -> None:
    res = read_map_resources(write_bsp(tmp_path / "de_test.bsp", ENTITIES, 3))
    assert res.brush_models == 3 and res.entities == 6
    assert res.models == {"sprites/glow01.spr", "models/props/barrel.mdl"}
    assert res.sounds == {"ambience/wind.wav"}  # "!sentence" and "3" are not files
    assert res.model_slots == 5


def test_model_refs_read_events_and_textures() -> None:
    refs = model_refs(_MINI)
    assert refs.version == 10 and not refs.error
    assert [t.name for t in refs.textures] == ["skin.bmp", "glass.bmp"]
    assert refs.sounds == {"weapons/x.wav": 5004}


def test_doctor_finds_missing_paths_case_and_duplicates(tmp_path: Path) -> None:
    mod = tmp_path / "cstrike"
    (mod / "models").mkdir(parents=True)
    (mod / "sound" / "weapons").mkdir(parents=True)
    shutil.copy(_MINI, mod / "models" / "v_Mini.mdl")  # plays weapons/x.wav: missing
    long_name = "x" * 70
    (mod / "sound" / f"{long_name}.wav").write_bytes(b"RIFF")
    (mod / "sound" / "weapons" / "a.wav").write_bytes(b"same bytes")
    (mod / "sound" / "weapons" / "b.wav").write_bytes(b"same bytes")
    issues = check_folder(mod)
    text = [(i.severity, i.category, i.path) for i in issues]
    assert ("error", "missing", "models/v_Mini.mdl") in text
    assert ("error", "path", f"sound/{long_name}.wav") in text
    assert ("warning", "case", "models/v_Mini.mdl") in text
    assert ("info", "duplicate", "sound/weapons/b.wav") in text
    assert summary(issues)["error"] == 2

    # the base game ships the sound: not missing any more
    (tmp_path / "valve" / "sound" / "weapons").mkdir(parents=True)
    (tmp_path / "valve" / "sound" / "weapons" / "x.wav").write_bytes(b"RIFF")
    assert not any(i.category == "missing" for i in check_folder(mod))


def test_budget_sums_map_project_and_extras() -> None:
    from valve_qc_merger.server.budget import BuildLoad
    load = ProjectLoad(builds=[BuildLoad("view", 39, ["models/v_a_p1.mdl", "models/v_a_p2.mdl"])],
                       client_sounds={"weapons/a.wav", "weapons/b.wav"})
    assert load.saved == 37
    res = MapResources("de_test", brush_models=40, models={"sprites/a.spr"},
                       sounds={"ambience/wind.wav"})
    lines = budget(res, load, extra={"models": 300, "sounds": 200, "generic": 10})
    assert lines["models"].used == 40 + 1 + 2 + 300 and lines["models"].limit == MAX_MODELS
    assert lines["sounds"].used == 1 + 200
    assert lines["generic"].used == 2 + 10 and lines["generic"].limit == MAX_GENERIC
    as_sound = budget(res, load, client_sounds_as="sound")
    assert as_sound["sounds"].used == 3 and as_sound["generic"].used == 0
    assert level(470, 512) == "warning" and level(513, 512) == "error"
    assert level(100, 512) == "ok"


def test_project_settings_round_trip_budget_fields(tmp_path: Path) -> None:
    from valve_qc_merger.project import Project
    project = Project.create(tmp_path / "pack")
    project.settings.extra_models = 180
    project.settings.client_sounds = "sound"
    project.save()
    again = Project.open(project.root)
    assert again.settings.extra_models == 180 and again.settings.client_sounds == "sound"


def test_server_window_budget_and_doctor(tmp_path: Path) -> None:
    import os

    import pytest
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from valve_qc_merger.project import Project
    from valve_qc_merger.studio.server_window import NO_MAP, ServerWindow

    mod = tmp_path / "cstrike"
    (mod / "maps").mkdir(parents=True)
    (mod / "models").mkdir()
    write_bsp(mod / "maps" / "de_test.bsp", ENTITIES, 30)
    shutil.copy(_MINI, mod / "models" / "v_mini.mdl")
    project = Project.create(tmp_path / "pack")
    project.settings.game_dir = str(mod)
    project.settings.extra_models = 470
    window = ServerWindow(project)
    assert [window.map_box.itemText(i) for i in range(window.map_box.count())] == \
        [NO_MAP, "de_test"]
    window.map_box.setCurrentText("de_test")
    models = window.bars["models"]
    assert models.numbers.text().startswith("502 / 512")  # 30 brush + 2 entity + 470
    assert models.bar.property("level") == "warning"
    window.extra["models"].setValue(500)  # saved to the project, budget redone
    assert Project.open(project.root).settings.extra_models == 500
    assert window.bars["models"].bar.property("level") == "error"
    assert window.maps_table.item(0, 4).text() == "over a limit"

    window.scan()  # mini.mdl plays weapons/x.wav, which is missing
    assert any(i.category == "missing" for i in window.issues)
    assert "1 error" in window.summary_label.text()
    window.severity_box.setCurrentIndex(window.severity_box.findData("info"))
    assert window.issues_table.rowCount() == 0
    assert "missing" in "\n".join(f"{i.category}" for i in window.issues)
