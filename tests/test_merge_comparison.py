"""Budget: the project's models as imported against after the merge."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from valve_qc_merger.project import Build, Project
from valve_qc_merger.project.model import Asset
from valve_qc_merger.server.budget import merge_comparison

_EXAMPLES = Path(__file__).parent / "examples"


def _project(tmp_path: Path) -> Project:
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_EXAMPLES / "v_anaconda", category="pistols")
    project.import_decompiled(_EXAMPLES / "pair_deagle" / "v_deagle", category="pistols")
    project.import_decompiled(_EXAMPLES / "pair_deagle" / "v_g_deagle", category="gold")
    for name in ("p_anaconda", "p_elite"):
        project.import_decompiled(_EXAMPLES / "player" / name)
    # a swap-hands copy is the same weapon: one slot, not two
    project.assets["v_anaconda_hands"] = Asset(
        "v_anaconda_hands", "v", project.assets["v_anaconda"].path, category="pistols",
        derived={"from": "v_anaconda", "mode": "hands", "options": {}})
    project.save()
    return project


def test_comparison_rows(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.add_build(Build("pistols", "merge-v", category="pistols", retarget=True))
    (project.build_dir("pistols")).mkdir(parents=True)
    (project.build_dir("pistols") / "last_run.json").write_text(json.dumps(
        {"outputs": ["builds/pistols/output/v_pistols.qc"]}), encoding="utf-8")
    project.add_build(Build("gold", "merge-v", category="gold"))
    (project.build_dir("gold")).mkdir(parents=True)
    (project.build_dir("gold") / "plan.json").write_text(json.dumps(
        {"parts": [{"part": "v_gold"}]}), encoding="utf-8")
    project.add_build(Build("held", "merge-p"))  # not run, no plan
    rows = {r.title: r for r in merge_comparison(project).rows}
    pistols = rows["pistols (merge-v)"]
    assert (pistols.imported, pistols.merged, pistols.state) == (2, 1, "run")
    gold = rows["gold (merge-v)"]
    assert (gold.imported, gold.merged, gold.state) == (1, 1, "planned")
    held = rows["held (merge-p)"]
    assert (held.imported, held.merged, held.state) == (2, 2, "not run")
    comparison = merge_comparison(project)
    assert comparison.imported == 5 and comparison.merged == 4
    assert comparison.pending == ["held (merge-p)"] and comparison.unmerged == 0
    # the run's manifest names what it took: v_deagle was left out
    output = project.build_dir("pistols") / "output"
    output.mkdir()
    (output / "models.ini").write_text("[v_anaconda_hands]\npev_body = 0\n",
                                       encoding="utf-8")
    pistols = {r.title: r for r in merge_comparison(project).rows}["pistols (merge-v)"]
    assert pistols.left_out == ["v_deagle"] and pistols.merged == 2  # 1 part + 1 as is


def test_models_in_no_build(tmp_path: Path) -> None:
    project = _project(tmp_path)
    comparison = merge_comparison(project)
    rows = {r.title: r for r in comparison.rows}
    # p_anaconda follows v_anaconda into pistols
    assert rows["pistols"].imported == 3 and rows["pistols"].state == "no build"
    assert rows["Uncategorized"].imported == 1 and rows["gold"].imported == 1
    assert comparison.imported == comparison.merged == comparison.unmerged == 5
    assert comparison.sounds_imported == comparison.sounds_merged


QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_budget_shows_both(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from tests.test_server import ENTITIES, write_bsp
    from valve_qc_merger.studio.server_window import ServerWindow
    project = _project(tmp_path)
    project.add_build(Build("pistols", "merge-v", category="pistols", retarget=True))
    project.build_dir("pistols").mkdir(parents=True)
    (project.build_dir("pistols") / "last_run.json").write_text(json.dumps(
        {"outputs": ["builds/pistols/output/v_pistols.qc"]}), encoding="utf-8")
    window = ServerWindow(project)
    try:
        table = window.merge_table
        cells = [[table.item(r, c).text() for c in range(5)] for r in range(table.rowCount())]
        assert cells[0] == ["pistols (merge-v)", "2", "1", "1", "run"]
        assert cells[-1][:4] == ["Total", "5", "4", "1"]
        assert "Merging saves 1 model slot(s): 5 as imported → 4." in \
            window.saved_label.text()
        window.add_map(write_bsp(tmp_path / "zm_x.bsp", ENTITIES, 100))
        text = window.verdict_text()
        # 100 brush + 2 entity + 120 stock + 4 after merge = 226 → 286 free; imported 5
        assert text.startswith("With zm_x, after merge: fits — 286 model slot(s) free")
        assert "As imported (no merge): fits — 285 model slot(s) free" in text
        row = next(r for r in range(window.maps_table.rowCount())
                   if window.maps_table.item(r, 0).text() == "zm_x")
        assert window.maps_table.item(row, 1).text() == "226 / 512"
        assert window.maps_table.item(row, 2).text() == "227 / 512"
    finally:
        window.close()
