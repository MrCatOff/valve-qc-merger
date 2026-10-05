"""Server logs: what a resource pack breaks, grouped, with advice."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from valve_qc_merger.server import logs

LOG = """L 10/05/2026 - 12:00:01: Host_Error: PF_precache_model_I: Model 'models/zm/v_axe.mdl' \
failed to precache because the item count is over the 512 limit.
SV_StartSound: weapons/zm_hit.wav not precached (0)
SV_StartSound: weapons/zm_hit.wav not precached (0)
L 10/05/2026 - 12:00:02: [AMXX] Plugin "zp_weapons.amxx" failed to load: Module/Library \
"reapi" required for plugin.  Check modules.ini.
L 10/05/2026 - 12:00:03: [AMXX] Plugin "broken.amxx" failed to load: Plugin uses an unknown \
function (name "foo")
L 10/05/2026 - 12:01:00: [AMXX] Run time error 4: index out of bounds (plugin "zp_buy.amxx") \
- debug not enabled!
ED_Alloc: no free edicts
Host_Error: Mod_NumForName: models/p_lost.mdl not found
Host_Error: Something odd happened
SZ_GetSpace: overflow on Client datagram
Host_Error: PF_precache_generic_I: 'sound/zm/a.wav' failed to precache because the item \
count is over the 4096 limit.
an ordinary line
"""


def test_analyze_groups_and_orders() -> None:
    found = logs.analyze(LOG, "L1005.log")
    keys = [(f.rule.key, f.subject) for f in found]
    assert keys[0] == ("model_limit", "models/zm/v_axe.mdl")  # errors in rule order
    assert ("generic_limit", "sound/zm/a.wav") in keys
    assert ("missing_model", "models/p_lost.mdl") in keys
    assert ("amxx_module", "zp_weapons.amxx") in keys and ("amxx_failed", "broken.amxx") \
        in keys
    module = next(f for f in found if f.rule.key == "amxx_module")
    assert module.detail == "reapi"
    sound = next(f for f in found if f.rule.key == "not_precached_sound")
    assert sound.count == 2 and sound.first_line == 2 and sound.lines == [2, 3]
    host = [f for f in found if f.rule.key == "host_error"]
    assert len(host) == 1 and host[0].detail == "Something odd happened"
    assert found[-1].rule.severity == "warning"
    text = logs.report(found)
    assert "[error] Model precache limit (512) reached models/zm/v_axe.mdl ×1 (L1005.log:1)" \
        in text


def test_log_files_and_merge(tmp_path: Path) -> None:
    game = tmp_path / "cstrike"
    (game / "logs").mkdir(parents=True)
    (game / "addons" / "amxmodx" / "logs").mkdir(parents=True)
    (game / "logs" / "L1.log").write_text("ED_Alloc: no free edicts\n", encoding="utf-8")
    (game / "addons" / "amxmodx" / "logs" / "error.log").write_text(
        "ED_Alloc: no free edicts\nED_Alloc: no free edicts\n", encoding="utf-8")
    files = logs.log_files(game)
    assert {p.name for p in files} == {"L1.log", "error.log"}
    merged = logs.analyze_files(files)
    assert len(merged) == 1 and merged[0].count == 3


QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_logs_tab(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from valve_qc_merger.project import Project
    from valve_qc_merger.studio.server_window import ServerWindow
    game = tmp_path / "cstrike"
    (game / "logs").mkdir(parents=True)
    (game / "logs" / "L1.log").write_text(LOG, encoding="utf-8")
    project = Project.create(tmp_path / "pack")
    project.settings.game_dir = str(game)
    window = ServerWindow(project)
    try:
        panel = window.logs
        found = panel.scan_folder()
        assert len(found) == panel.table.rowCount() >= 9
        assert "1 log file(s)" in panel.summary.text()
        assert panel.goto_button.isVisible() is False or panel.goto_button.text() == \
            "Open Budget"
        panel.goto_button.click()
        assert window.tabs.currentIndex() == 0  # Budget
        row = next(i for i, f in enumerate(found) if f.rule.key == "edicts")
        panel.table.selectRow(row)
        assert panel.goto_button.text() == "Open Entities"
        panel.goto_button.click()
        assert window.tabs.currentWidget() is window.entities
        panel.analyze_text("nothing here\n", "clipboard")
        assert "nothing a resource pack explains" in panel.summary.text()
    finally:
        window.close()
