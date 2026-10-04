"""game.cfg / server.cfg: documented cvars, in-place edits, the Config tab."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from valve_qc_merger.server import cfg

_SAMPLE = """// my server
echo hello

// Auto balancing of teams
// 0 - disabled
// 1 - on after next round
//
// Default value: "1"
mp_autoteambalance "0"   // off here
mp_buytime 0.5
sv_password "secret"
"""


def test_parse_reads_docs_and_values() -> None:
    cvars = {c.name: c for c in cfg.parse(_SAMPLE)}
    assert set(cvars) == {"mp_autoteambalance", "mp_buytime"}  # no echo, no password
    team = cvars["mp_autoteambalance"]
    assert team.value == "0" and team.default == "1" and team.changed
    assert team.description == "Auto balancing of teams"
    assert team.options == [("0", "disabled"), ("1", "on after next round")]
    assert cvars["mp_buytime"].value == "0.5" and cvars["mp_buytime"].default is None


def test_references_are_bundled_and_documented() -> None:
    game, server = cfg.reference("game"), cfg.reference("server")
    assert len(game) > 80 and len(server) > 40
    assert all(c.default is not None for c in game + server)
    infinite = next(c for c in game if c.name == "mp_round_infinite")
    assert infinite.options[0][0] == "0" and any(n.startswith("a - ") for n in infinite.notes)
    names = {c.name for c in server}
    assert {"sv_rehlds_force_dlmax", "sv_auto_precache_sounds_in_models"} <= names
    assert cfg.group_of("mp_buytime") == "mp" and cfg.group_of("ff_damage_reduction_bullets") \
        == "other"


def test_documented_fills_from_reference() -> None:
    cvars = cfg.documented('mp_freeforall "1"\n', "game")
    first = cvars[0]
    assert first.name == "mp_freeforall" and first.value == "1" and first.default == "0"
    assert "FFA" in first.description
    rest = {c.name: c for c in cvars[1:]}
    assert "mp_freeforall" not in rest and rest["mp_maxmoney"].value == "16000"


def test_cfgfile_edits_in_place(tmp_path: Path) -> None:
    document = cfg.CfgFile(_SAMPLE)
    assert document.get("MP_BUYTIME") == "0.5" and document.get("nope") is None
    document.set("mp_buytime", "1")
    document.set("mp_autoteambalance", "2")
    doc = cfg.Cvar("mp_freeforall", "1", "0", "FFA mode", [("0", "off"), ("1", "on")])
    document.set("mp_freeforall", "1", doc=doc)
    text = document.text()
    assert 'mp_autoteambalance "2"   // off here' not in text  # spacing normalised
    assert 'mp_autoteambalance "2" // off here' in text and 'mp_buytime "1"' in text
    assert text.startswith("// my server\necho hello\n") and 'sv_password "secret"' in text
    assert text.endswith('// FFA mode\n// 0 - off\n// 1 - on\n// Default value: "0"\n'
                         'mp_freeforall "1"\n')
    path = tmp_path / "game.cfg"
    assert document.save(path) is None
    assert document.save(path) == tmp_path / "game.cfg.bak"
    assert cfg.CfgFile.load(path).get("mp_freeforall") == "1"


QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_config_tab(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from valve_qc_merger.project import Project
    from valve_qc_merger.studio.server_window import ServerWindow
    game = tmp_path / "cstrike"
    game.mkdir()
    (game / "game.cfg").write_text('// mine\nmp_freeforall "1"\n', encoding="utf-8")
    project = Project.create(tmp_path / "pack")
    project.settings.game_dir = str(game)
    window = ServerWindow(project)
    try:
        panel = window.config
        assert panel.path() == game / "game.cfg" and panel.cvars[0].name == "mp_freeforall"
        panel.search.setText("freeforall")
        assert panel.visible_names() == ["mp_freeforall"]
        panel.search.setText("")
        panel.changed_box.setChecked(True)
        assert panel.visible_names() == ["mp_freeforall"]  # 1 vs default 0
        panel.set_value("mp_maxmoney", "999999")
        assert [c.name for c in panel.edits()] == ["mp_maxmoney"]
        panel.save()
        text = (game / "game.cfg").read_text(encoding="utf-8")
        assert text.startswith('// mine\nmp_freeforall "1"\n') and 'mp_maxmoney "999999"' in text
        assert (game / "game.cfg.bak").is_file() and not panel.edits()
        # server.cfg does not exist: only the edits are written, documented
        panel.kind_box.setCurrentIndex(panel.kind_box.findData("server"))
        assert panel.path() == game / "server.cfg"
        panel.set_value("sv_rehlds_force_dlmax", "1")
        panel.save()
        server = (game / "server.cfg").read_text(encoding="utf-8")
        assert server.rstrip().endswith('sv_rehlds_force_dlmax "1"') and "// " in server
        assert server.count('"') == 4  # one documented cvar: default + value
    finally:
        window.close()


def test_new_game_cfg_starts_from_reference(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from valve_qc_merger.project import Project
    from valve_qc_merger.studio.config_panel import ConfigPanel
    project = Project.create(tmp_path / "pack")
    project.settings.game_dir = str(tmp_path)
    panel = ConfigPanel(project)
    panel.set_value("mp_freeforall", "1")
    panel.save()
    saved = cfg.parse((tmp_path / "game.cfg").read_text(encoding="utf-8"))
    values = {c.name: c.value for c in saved}
    assert values["mp_freeforall"] == "1" and values["mp_buytime"] == "1.5"  # defaults
    assert not (tmp_path / "game.cfg").read_text(encoding="utf-8").startswith("// Reference")
