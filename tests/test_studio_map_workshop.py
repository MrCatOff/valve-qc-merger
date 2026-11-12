"""Map Workshop (offscreen Qt): every tab shows, changes go into the plan,
undo / redo, Apply writes and keeps the CRC."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from tests.mapkit_fixtures import build_map
from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.server.entities import parse

QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _map(tmp_path: Path) -> Path:
    game = tmp_path / "cstrike"
    (game / "maps").mkdir(parents=True)
    entities = [{"classname": "worldspawn"},
                {"classname": "info_player_start", "origin": "-400 0 37", "angles": "0 0 0"},
                {"classname": "info_player_deathmatch", "origin": "400 0 37", "angles": "0 0 0"},
                {"classname": "particle_emitter", "origin": "0 0 64"},
                {"classname": "cycler_sprite", "model": "models/gone.mdl", "origin": "1 1 0"}]
    path = game / "maps" / "zm_ws.bsp"
    path.write_bytes(build_map(entities, external=[]))
    return path


def _wait(window, app) -> None:  # noqa: ANN001
    assert window.jobs.wait(60_000)
    app.processEvents()


@pytest.fixture()
def workshop(app, tmp_path: Path):  # noqa: ANN001, ANN201
    from valve_qc_merger.studio.map_workshop import MapWorkshop
    window = MapWorkshop([], None)
    window.state.rules.min_spawns = 3
    window.state.rules.max_players = 6
    yield window
    window.close()


def test_every_tab_shows_a_map(workshop, app, tmp_path: Path) -> None:  # noqa: ANN001
    state = workshop.open_map(_map(tmp_path))
    assert workshop.current() is state
    codes = [workshop.issues.item(r, 1).text() for r in range(workshop.issues.rowCount())]
    assert "missing_model" in codes and "unknown_class" in codes
    for index in range(workshop.tabs.count()):
        workshop.tabs.setCurrentIndex(index)
        app.processEvents()
    assert workshop.plan_view.spawns and len(workshop.plan_view.spawns) == 2
    assert workshop.entity_table.rowCount() == 5
    assert workshop.texture_list.count() == 2


def test_changes_are_planned_undone_and_applied(workshop, app, tmp_path: Path) -> None:  # noqa: ANN001
    path = _map(tmp_path)
    state = workshop.open_map(path)
    crc = B.Bsp.read(path).crc()
    workshop._fix(True)
    codes = {op.code for op in state.plan.operations}
    assert {"cso.remove", "entity.remove", "spawn.generate"} <= codes
    count = len(state.plan.operations)
    workshop._history(-1)
    assert not state.plan.operations
    workshop._history(1)
    assert len(state.plan.operations) == count
    # an entity's key edited in the table goes into the plan
    workshop.tabs.setCurrentIndex(3)
    workshop._select_entity(1)
    app.processEvents()
    for row in range(workshop.key_table.rowCount()):
        if workshop.key_table.item(row, 0).text() == "angles":
            workshop.key_table.item(row, 1).setText("0 90 0")
    assert state.plan.operations[-1].data == {"key": "angles", "value": "0 90 0"}
    assert "the CRC is kept" in workshop.plan_label.text()
    workshop.apply()
    written = parse(B.Bsp.read(path).entity_text)
    assert B.Bsp.read(path).crc() == crc
    assert not any(e.get("classname") == "particle_emitter" for e in written)
    assert sum(e.get("classname") == "info_player_start" for e in written) == 3
    assert next(e for e in written if e.get("origin") == "-400 0 37")["angles"] == "0 90 0"
    assert path.with_name("zm_ws.bsp.bak").is_file()
    assert not workshop.current().plan.operations  # a fresh plan on the written map


def test_a_dropped_picture_replaces_a_texture(workshop, app, tmp_path: Path) -> None:  # noqa: ANN001
    from valve_qc_merger.preview.render import encode_png
    path = _map(tmp_path)
    state = workshop.open_map(path)
    picture = tmp_path / "red.png"
    rgba = np.zeros((16, 16, 4), np.uint8)
    rgba[..., 0], rgba[..., 3] = 250, 255
    picture.write_bytes(encode_png(rgba))
    workshop._texture_dropped("floor_a", str(picture))
    assert state.plan.operations[-1].code == "textures.replace"
    assert "the CRC changes" in workshop.plan_label.text() and "zm_ws_sp" in \
        workshop.plan_label.text()
    workshop.apply()
    assert path.with_name("zm_ws_sp.bsp").is_file()
    assert workshop.current().name == "zm_ws_sp"


def test_autospawns_run_in_the_background(workshop, app, tmp_path: Path) -> None:  # noqa: ANN001
    from valve_qc_merger.mapkit.spawns import SpawnOptions, generate
    state = workshop.open_map(_map(tmp_path))
    workshop._pending["spawns"] = state
    entities = state.planned_entities()
    workshop.jobs.start("Autospawns", lambda r: generate(state.bsp, entities,
                                                         SpawnOptions(per_team=4)))
    _wait(workshop, app)
    assert sum(op.code == "spawn.generate" for op in state.plan.operations) == 6


def test_the_main_window_opens_it(app, tmp_path: Path) -> None:  # noqa: ANN001
    from PySide6.QtCore import QSettings

    from valve_qc_merger.studio.main_window import MainWindow
    window = MainWindow(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    try:
        window.show_map_workshop(_map(tmp_path))
        assert window.map_workshop.current().name == "zm_ws"
    finally:
        window.map_workshop.close()
        window.close()
