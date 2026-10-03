"""QC edits on an asset: sequences, events, render modes, raw text."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from valve_qc_merger.merge_view.discovery import load_model
from valve_qc_merger.project.qc_edit import (
    QcEditError,
    SequenceEvent,
    parse_blocks,
    qc_file,
    set_render_mode,
    set_sequence,
    write_qc,
)
from valve_qc_merger.retarget.qc_build import parse_sequences

_ANACONDA = Path("tests/examples/v_anaconda")


@pytest.fixture()
def asset(tmp_path: Path) -> Path:
    target = tmp_path / "v_anaconda"
    shutil.copytree(_ANACONDA, target)
    return target


def test_parse_blocks_both_forms() -> None:
    text = ('$sequence "idle" {\n\t"a/idle"\n\tblend XR -45 45\n'
            '\t{ event 5004 3 "x.wav" }\n\tfps 15\n\tloop\n\tACT_IDLE 2\n}\n'
            '$sequence die "./anims/die" fps 16 LX\n')
    idle, die = parse_blocks(text)
    assert (idle.name, idle.paths, idle.fps, idle.loop, idle.activity, idle.other) == \
        ("idle", ["a/idle"], 15.0, True, "ACT_IDLE 2", "blend XR -45 45")
    assert idle.events == [SequenceEvent(5004, 3, "x.wav")]
    assert (die.name, die.paths, die.fps, die.other) == ("die", ["./anims/die"], 16.0, "LX")


def test_set_sequence_edits_one_block(asset: Path) -> None:
    before = parse_sequences(qc_file(asset).read_text(encoding="latin-1"))
    set_sequence(asset, 1, name="fire", fps=24, loop=True, activity="ACT_VM_PRIMARYATTACK 1",
                 events=[SequenceEvent(5004, 10, "weapons/new.wav"), SequenceEvent(5001, 0, "21")])
    after = parse_sequences(qc_file(asset).read_text(encoding="latin-1"))
    assert [s.name for s in after] == [before[0].name, "fire", *[s.name for s in before[2:]]]
    fire = after[1]
    assert (fire.fps, fire.loop, fire.activity, fire.smd) == \
        (24.0, True, "ACT_VM_PRIMARYATTACK 1", before[1].smd)
    assert fire.events == ('{ event 5001 0 "21" }', '{ event 5004 10 "weapons/new.wav" }')
    assert after[2] == before[2]  # neighbours untouched
    load_model(asset)  # still a valid model
    set_sequence(asset, 1, fps=None, activity=None)
    assert parse_sequences(qc_file(asset).read_text(encoding="latin-1"))[1].fps is None
    with pytest.raises(QcEditError):
        set_sequence(asset, 1, name=before[0].name)  # duplicate
    with pytest.raises(QcEditError):
        set_sequence(asset, 1, name="x" * 40)
    with pytest.raises(QcEditError):
        set_sequence(asset, 99, fps=10)


def test_render_modes(asset: Path) -> None:
    set_render_mode(asset, "Anaconda_512.BMP", "masked")
    text = qc_file(asset).read_text(encoding="latin-1")
    assert text.count('$texrendermode "Anaconda_512.BMP" masked') == 1
    set_render_mode(asset, "anaconda_512.bmp", "additive")  # case-insensitive, replaces
    text = qc_file(asset).read_text(encoding="latin-1")
    assert "masked" not in text and text.count("$texrendermode") == 1
    assert text.index("$texrendermode") < text.index("$bodygroup")
    set_render_mode(asset, "Anaconda_512.BMP", None)
    assert "$texrendermode" not in qc_file(asset).read_text(encoding="latin-1")
    with pytest.raises(QcEditError):
        set_render_mode(asset, "x.bmp", "glow")


def test_write_qc_refuses_a_broken_model(asset: Path) -> None:
    good = qc_file(asset).read_text(encoding="latin-1")
    write_qc(asset, good.replace("fps 16", "fps 20", 1))
    assert "fps 20" in qc_file(asset).read_text(encoding="latin-1")
    with pytest.raises(QcEditError, match="no longer loads"):
        write_qc(asset, good.replace("ref_Anaconda", "missing_mesh"))
    assert "ref_Anaconda" in qc_file(asset).read_text(encoding="latin-1")  # put back


QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_studio_qc_tools(tmp_path: Path, monkeypatch) -> None:
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings

    from valve_qc_merger.project import Project
    from valve_qc_merger.studio import qc_tools
    from valve_qc_merger.studio.main_window import MainWindow
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    src = tmp_path / "src" / "v_anaconda"
    shutil.copytree(_ANACONDA, src)
    shutil.copy(src / "Anaconda_512.BMP", src / "Anaconda_gold.bmp")
    qc = src / "v_anaconda.qc"
    qc.write_text(qc.read_text(encoding="latin-1").replace(
        "$flags 0", '$flags 0\n$texturegroup "skinfamilies"\n{\n\t{ "Anaconda_512.BMP" }\n'
        '\t{ "Anaconda_gold.bmp" }\n}\n'), encoding="latin-1")
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(src)
    win = MainWindow(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    try:
        win.set_project(project)
        assert win.explorer.select("asset", "v_anaconda")
        inspector, panel = win.inspector, win.viewport
        # skins: the page lists both rows, picking one retextures the viewport
        assert inspector.skins_page.table.rowCount() == 2
        inspector.skins_page.table.selectRow(1)
        assert any(b.material == "Anaconda_gold.bmp" for b in panel.viewport.scene.batches)
        assert panel.group_boxes["\x00skin"].currentIndex() == 1
        panel.set_skin(0)
        assert inspector.skins_page.table.currentRow() == 0

        # sequence dialog -> set_sequence (as a job, snapshotted)
        def accept(self):  # noqa: ANN001
            self.fps_spin.setValue(25)
            self.loop_box.setChecked(True)
            self._add_row(SequenceEvent(5004, 1, "weapons/x.wav"))
            return qc_tools.SequenceDialog.DialogCode.Accepted
        monkeypatch.setattr(qc_tools.SequenceDialog, "exec", accept)
        inspector.sequences.cellDoubleClicked.emit(0, 1)
        assert win.jobs.wait(30_000)
        idle = parse_sequences(qc_file(project.asset_dir("v_anaconda")).read_text(
            encoding="latin-1"))[0]
        assert idle.fps == 25.0 and idle.loop and '{ event 5004 1 "weapons/x.wav" }' in idle.events
        assert project.can_undo("v_anaconda")

        # render mode via the Textures table
        monkeypatch.setattr(QtWidgets.QInputDialog, "getItem",
                            lambda *a, **k: ("additive", True))
        win.edit_render_mode("Anaconda_512.BMP")
        assert win.jobs.wait(30_000)
        text = qc_file(project.asset_dir("v_anaconda")).read_text(encoding="latin-1")
        assert '$texrendermode "Anaconda_512.BMP" additive' in text

        # raw QC: a broken edit is refused, the file stays
        page = inspector.qc_page
        assert page.editor.toPlainText() == text and not page.dirty
        page.editor.setPlainText(text.replace("ref_Anaconda", "nope"))
        assert page.dirty and page.save_button.isEnabled()
        page.save_button.click()
        assert win.jobs.wait(30_000)
        assert qc_file(project.asset_dir("v_anaconda")).read_text(encoding="latin-1") == text
        win.undo_asset_edit()  # back before the render mode edit
        assert "$texrendermode" not in qc_file(project.asset_dir("v_anaconda")).read_text(
            encoding="latin-1")
    finally:
        win.close()
