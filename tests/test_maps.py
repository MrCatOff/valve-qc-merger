"""Map needs (.res), the mapcycle and the Maps tab."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.test_server import write_bsp
from valve_qc_merger.server import maps
from valve_qc_merger.server.scan import scan_folder

WORLD = """{
"classname" "worldspawn"
"wad" "\\\\half-life\\\\cstrike\\\\zm_pack.wad;\\\\half-life\\\\valve\\\\halflife.wad;gone.wad"
"skyname" "night"
}
{
"classname" "cycler_sprite"
"model" "models/zm/box.mdl"
}
"""


def _server(tmp_path: Path) -> Path:
    game = tmp_path / "cstrike"
    (game / "maps").mkdir(parents=True)
    write_bsp(game / "maps" / "zm_test.bsp", WORLD, 2)
    write_bsp(game / "maps" / "de_plain.bsp", '{\n"classname" "worldspawn"\n}\n', 1)
    (game / "zm_pack.wad").write_bytes(b"WAD3")
    (game / "gfx" / "env").mkdir(parents=True)
    for side in maps.SKY_SIDES[:5]:  # "bk" is missing
        (game / "gfx" / "env" / f"night{side}.tga").write_bytes(b"x")
    (game / "maps" / "zm_test_detail.txt").write_text("{tex detail/sand 2 2\n",
                                                      encoding="latin-1")
    (game / "gfx" / "detail").mkdir()
    (game / "gfx" / "detail" / "sand.tga").write_bytes(b"x")
    (game / "models" / "zm").mkdir(parents=True)
    (game / "models" / "zm" / "box.mdl").write_bytes(b"x")
    (game / "mapcycle.txt").write_text("zm_test\n// note\nde_gone 30\n", encoding="latin-1")
    valve = tmp_path / "valve"
    valve.mkdir()
    (valve / "halflife.wad").write_bytes(b"WAD3")
    return game


def test_map_needs_and_res(tmp_path: Path) -> None:
    game = _server(tmp_path)
    needs = maps.map_needs(game / "maps" / "zm_test.bsp")
    assert needs.res[:3] == ["zm_pack.wad", "halflife.wad", "gone.wad"]
    assert "gfx/env/nightbk.tga" in needs.res and "gfx/detail/sand.tga" in needs.res
    assert "maps/zm_test_detail.txt" in needs.res
    assert needs.precached == ["models/zm/box.mdl"] and needs.model_slots == 3
    check = maps.check_map(game / "maps" / "zm_test.bsp", scan_folder(game),
                           [scan_folder(tmp_path / "valve")])
    assert check.missing == ["gone.wad", "gfx/env/nightbk.tga"]
    assert check.stock == ["halflife.wad"] and check.res_state == "none"
    assert "halflife.wad" not in check.to_write and "zm_pack.wad" in check.to_write
    res = maps.write_res(check, game / "maps" / "zm_test.bsp")
    assert res == game / "maps" / "zm_test.res"
    assert maps.parse_res(res.read_text(encoding="latin-1")) == check.to_write
    again = maps.check_map(game / "maps" / "zm_test.bsp", scan_folder(game),
                           [scan_folder(tmp_path / "valve")])
    assert again.res_state == "ok"
    res.write_text(res.read_text(encoding="latin-1") + "sound/nope.wav\n", encoding="latin-1")
    stale = maps.check_map(game / "maps" / "zm_test.bsp", scan_folder(game), [])
    assert stale.res_state == "outdated" and stale.res_extra == ["sound/nope.wav"]
    plain = maps.check_map(game / "maps" / "de_plain.bsp", scan_folder(game), [])
    assert not plain.to_write and maps.write_res(plain, game / "maps" / "de_plain.bsp") is None
    assert maps.read_mapcycle(game / "mapcycle.txt") == ["zm_test", "de_gone"]


def test_doctor_reports_a_missing_sky(tmp_path: Path) -> None:
    from valve_qc_merger.server.doctor import check_folder
    game = _server(tmp_path)
    messages = [i.message for i in check_folder(game) if i.path == "maps/zm_test.bsp"]
    assert any("gfx/env/nightbk.tga" in m for m in messages)
    assert any("lists gone.wad" in m for m in messages)


QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_maps_tab(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from valve_qc_merger.project import Project
    from valve_qc_merger.studio.server_window import ServerWindow
    game = _server(tmp_path)
    project = Project.create(tmp_path / "pack")
    project.settings.game_dir = str(game)
    window = ServerWindow(project)
    try:
        panel = window.maps_panel
        panel.scan()
        assert panel.shown() == ["de_gone", "de_plain", "zm_test"]
        assert "1 in the cycle without a BSP" in panel.summary.text()
        panel.cycle_box.setChecked(True)
        assert panel.shown() == ["de_gone", "zm_test"]
        rows = {panel.table.item(r, 0).text(): r for r in range(panel.table.rowCount())}
        assert panel.table.item(rows["de_gone"], 6).text() == "no BSP"
        assert panel.table.item(rows["zm_test"], 6).text() == "none"
        assert panel.table.item(rows["zm_test"], 5).text() == "2"
        written = panel.write_selected(["zm_test"])
        assert written == [game / "maps" / "zm_test.res"]
        rows = {panel.table.item(r, 0).text(): r for r in range(panel.table.rowCount())}
        assert panel.table.item(rows["zm_test"], 6).text() == "up to date"
        panel.table.selectRow(rows["zm_test"])
        statuses = [panel.details.item(r, 2).text() for r in range(panel.details.rowCount())]
        assert "missing (res)" in statuses and "base game (every client has it)" in statuses
        assert "missing gone.wad" in panel.report()
    finally:
        window.close()
