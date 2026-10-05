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


def test_map_wads_and_doctor_map_checks(tmp_path: Path) -> None:
    from valve_qc_merger.server.bsp import wad_names
    assert wad_names("\\half-life\\cstrike\\cs_dust.wad;C:/x/Halflife.wad;;cs_dust.wad") \
        == ["cs_dust.wad", "Halflife.wad"]
    mod = tmp_path / "cstrike"
    (mod / "maps").mkdir(parents=True)
    write_bsp(mod / "maps" / "de_test.bsp", ENTITIES, 3)
    (mod / "sprites").mkdir()
    (mod / "sprites" / "glow01.spr").write_bytes(b"IDSP")
    valve = tmp_path / "valve"
    valve.mkdir()
    (valve / "halflife.wad").write_bytes(b"WAD3")  # the base game has it
    found = [i for i in check_folder(mod) if i.path == "maps/de_test.bsp"]
    text = [(i.severity, i.message) for i in found]
    assert ("error", "an entity uses models/props/barrel.mdl, which is not in the folder") \
        in text
    assert any(s == "warning" and "sound/ambience/wind.wav" in m for s, m in text)
    assert not any("halflife.wad" in m for _s, m in text) and len(found) == 2
    (valve / "halflife.wad").unlink()
    assert any("lists halflife.wad" in i.message for i in check_folder(mod))


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
    project.settings.extra_models = 350
    window = ServerWindow(project)
    assert [window.map_box.itemText(i) for i in range(window.map_box.count())] == \
        [NO_MAP, "de_test"]
    window.map_box.setCurrentText("de_test")
    models = window.bars["models"]
    # 30 brush + 2 entity + 120 stock CS models + 350 estimated
    assert models.numbers.text().startswith("502 / 512")
    assert models.bar.property("level") == "warning"
    assert window.verdict.text() == "With de_test: fits — 10 model slot(s) free."
    window.stock_box.setChecked(False)
    assert window.bars["models"].numbers.text().startswith("382 / 512")
    assert not Project.open(project.root).settings.count_stock
    window.stock_box.setChecked(True)
    window.extra["models"].setValue(500)  # saved to the project, budget redone
    assert Project.open(project.root).settings.extra_models == 500
    assert window.bars["models"].bar.property("level") == "error"
    assert window.verdict.text().startswith("With de_test: over the limit — models by 140")
    assert window.maps_table.item(0, 4).text() == "over a limit"

    window.scan()  # mini.mdl plays weapons/x.wav; the map's barrel and glow are missing
    assert any(i.category == "missing" for i in window.issues)
    assert "3 errors" in window.summary_label.text()
    window.severity_box.setCurrentIndex(window.severity_box.findData("info"))
    assert window.issues_table.rowCount() == 0
    assert "missing" in "\n".join(f"{i.category}" for i in window.issues)


def test_export_package_writes_tree_include_res_and_report(tmp_path: Path) -> None:
    import json

    import numpy as np

    from valve_qc_merger.project import Build, Project
    from valve_qc_merger.server.package import export_package
    from valve_qc_merger.sound.wav import write_wav
    project = Project.create(tmp_path / "pack")
    project.add_build(Build("view", "merge-v", options={"name": "v_pack"}))
    output = project.build_dir("view") / "output" / "p1"
    output.mkdir(parents=True)
    (output / "v_pack_p1.qc").write_text(
        '$modelname "v_pack_p1.mdl"\n$sequence "reload" {\n "a"\n'
        ' { event 5004 3 "weapons/pack_clipin.wav" }\n'
        ' { event 5004 9 "weapons/nowhere.wav" }\n}\n', encoding="latin-1")
    (output / "v_pack_p1.mdl").write_bytes(b"IDST" + b"\0" * 300)
    (project.build_dir("view") / "output" / "models.ini").write_text(
        "[v_ak47]\nmodel = v_pack_p1.mdl\npev_body = 3\nanim_reload = 1\nanim_draw = 2\n",
        encoding="utf-8")
    (project.build_dir("view") / "last_run.json").write_text(json.dumps(
        {"outputs": ["builds/view/output/p1/v_pack_p1.qc"]}), encoding="utf-8")
    sound = project.root / "sounds" / "weapons" / "pack_clipin.wav"
    sound.parent.mkdir(parents=True)
    sound.write_bytes(write_wav(np.zeros((100, 1)), 22050))

    result = export_package(project, tmp_path / "out")
    mod = tmp_path / "out" / "cstrike"
    assert (mod / "models" / "v_pack_p1.mdl").is_file()
    assert (mod / "models" / "v_pack_models.ini").is_file()
    assert (mod / "sound" / "weapons" / "pack_clipin.wav").is_file()
    assert result.models == ["models/v_pack_p1.mdl"]
    assert result.missing_sounds == ["weapons/nowhere.wav"]
    inc = (tmp_path / "out" / "amxx" / "vqm_resources.inc").read_text()
    assert '"models/v_pack_p1.mdl",' in inc
    assert 'precache_generic(VQM_CLIENT_SOUNDS[i]);' in inc  # ReHLDS default
    assert '"sound/weapons/pack_clipin.wav",' in inc
    assert '#define VQM_V_AK47_MODEL "models/v_pack_p1.mdl"' in inc
    assert "#define VQM_V_AK47_BODY 3" in inc and "#define VQM_V_AK47_ANIM_RELOAD 1" in inc
    res = (tmp_path / "out" / "vqm_resources.res").read_text().splitlines()
    assert "models/v_pack_p1.mdl" in res and "sound/weapons/pack_clipin.wav" in res
    text = (tmp_path / "out" / "package_report.txt").read_text()
    assert "sound/weapons/nowhere.wav" in text and "FastDL" in text
    checks = (tmp_path / "out" / "rechecker" / "resources.ini").read_text()
    assert '"models/v_pack_p1.mdl"' in checks and "IGNORE" in checks

    project.settings.client_sounds = "sound"
    inc = export_package(project, tmp_path / "out2")
    text = (tmp_path / "out2" / "amxx" / "vqm_resources.inc").read_text()
    assert 'precache_sound(VQM_CLIENT_SOUNDS[i]);' in text
    assert '"weapons/pack_clipin.wav",' in text


# -- unprecache ----------------------------------------------------------------
def test_unprecache_list_and_slots(tmp_path: Path) -> None:
    from valve_qc_merger.server.stock import GROUPS, existing
    from valve_qc_merger.server.unprecache import Entry, list_ini, parse_list_ini, slots_freed
    world = GROUPS["w"][2]
    assert "models/w_ak47.mdl" in world and "models/w_knife.mdl" not in world
    entries = [Entry(p, "models/w_supplybox.mdl") for p in world[:5]]
    assert slots_freed(entries) == 5 - 1  # the supply box takes one slot
    assert slots_freed(entries, {"models/w_supplybox.mdl"}) == 5
    assert slots_freed([Entry("models/v_ak47.mdl")]) == 1
    text = list_ini(entries[:2] + [Entry("models/v_ak47.mdl")], title="pack")
    assert "models/w_ak47.mdl c models/w_supplybox.mdl" in text
    assert "\nmodels/v_ak47.mdl\n" in text
    assert parse_list_ini(text) == entries[:2] + [Entry("models/v_ak47.mdl")]
    mod = tmp_path / "cstrike" / "models"
    mod.mkdir(parents=True)
    (mod / "w_ak47.mdl").write_bytes(b"IDST")
    assert existing(world, tmp_path / "cstrike") == ["models/w_ak47.mdl"]
    assert existing(world, None) == list(world)


def test_unprecache_tab(tmp_path: Path, monkeypatch) -> None:
    import os

    import pytest
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from PySide6.QtCore import Qt

    from valve_qc_merger.project import Project
    from valve_qc_merger.studio import server_window
    project = Project.create(tmp_path / "pack")
    project.settings.extra_models = 300
    window = server_window.ServerWindow(project)
    tree = window.unprecache_tree
    world = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                 if tree.topLevelItem(i).text(0).startswith("World models"))
    world.setCheckState(0, Qt.CheckState.Checked)  # ticks every w_ model
    count = world.childCount()
    window.replace_box.setChecked(True)
    window.replace_edit.setText("models/w_supplybox.mdl")
    window.replace_edit.editingFinished.emit()
    assert window.freed_slots() == count - 1
    assert "frees" in window.freed_label.text()
    again = Project.open(project.root)
    assert len(again.settings.unprecache) == count
    assert again.settings.unprecache_replace == "models/w_supplybox.mdl"
    parts = dict(window._lines(None)["models"].parts)
    assert parts["unprecached stock models"] == -(count - 1)
    target = tmp_path / "list.ini"
    monkeypatch.setattr(server_window.QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(target), ""))
    assert window.export_unprecache() == str(target)
    assert "models/w_ak47.mdl c models/w_supplybox.mdl" in target.read_text()


def test_rechecker_rules(tmp_path: Path) -> None:
    import hashlib

    from valve_qc_merger.server.rechecker import rules, short_hash
    model = tmp_path / "v_pack.mdl"
    model.write_bytes(b"IDST model bytes")
    sound = tmp_path / "x.wav"
    sound.write_bytes(b"RIFF")
    assert short_hash(model) == hashlib.md5(b"IDST model bytes").hexdigest()[:8]
    text = rules({"models/v_pack.mdl": model, "sound/x.wav": sound})
    lines = [line for line in text.splitlines() if not line.startswith(";")]
    assert len(lines) == 2  # sounds are left out by default
    assert lines[0].startswith('"models/v_pack.mdl"') and lines[0].endswith(
        f"{short_hash(model)}\tIGNORE")
    assert "UNKNOWN" in lines[1] and "kick [userid]" in lines[1] and lines[1].endswith("BREAK")
    with_sounds = rules({"models/v_pack.mdl": model, "sound/x.wav": sound},
                        include_sounds=True)
    assert '"sound/x.wav"' in with_sounds
