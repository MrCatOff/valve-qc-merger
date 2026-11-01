"""Replacing the sounds a weapon's events play, without editing the QC."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from valve_qc_merger.project import Project
from valve_qc_merger.project.qc_edit import model_sounds, parse_blocks, qc_file, replace_sounds

_ANACONDA = Path(__file__).parent / "examples" / "v_anaconda"  # weapons/ana_foley1..5.wav


def test_sounds_are_replaced_in_every_sequence(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_ANACONDA)
    directory = project.asset_dir("v_anaconda")
    sounds = model_sounds(directory)
    assert "weapons/ana_foley1.wav" in sounds and all(uses for uses in sounds.values())
    before = qc_file(directory).read_text(encoding="latin-1")
    result = replace_sounds(directory, {"WEAPONS/ana_foley1.wav": "weapons/my_clipin.wav"})
    after = qc_file(directory).read_text(encoding="latin-1")
    assert not result.warnings and "ana_foley1.wav" not in after
    assert after.count("weapons/my_clipin.wav") == before.count("ana_foley1.wav")
    assert len(parse_blocks(after)) == len(parse_blocks(before))  # every sequence kept
    assert [b.paths for b in parse_blocks(after)] == [b.paths for b in parse_blocks(before)]
    with pytest.raises(ValueError):
        replace_sounds(directory, {"weapons/ana_foley2.wav": 'bad"name.wav'})


QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_the_dialogs_pick_sounds(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from valve_qc_merger.project.qc_edit import SequenceEvent
    from valve_qc_merger.studio.model_sounds import ModelSoundsDialog, SoundPicker
    from valve_qc_merger.studio.qc_tools import SequenceDialog
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_ANACONDA)
    directory = project.asset_dir("v_anaconda")
    dialog = ModelSoundsDialog(project, "v_anaconda", model_sounds(directory))
    assert dialog.mapping() == {}  # nothing changed yet
    dialog.pickers[0].set_value("weapons/other.wav")
    assert dialog.mapping() == {dialog.sounds[0]: "weapons/other.wav"}
    block = next(b for b in parse_blocks(qc_file(directory).read_text("latin-1")) if b.events)
    sequence = SequenceDialog(block, 40, project=project, current_frame=7)
    sounds = [r for r in range(sequence.events.rowCount())
              if isinstance(sequence.events.cellWidget(r, 2), SoundPicker)]
    assert sounds  # a sound event's option is a picker
    sequence.events.cellWidget(sounds[0], 2).set_value("weapons/picked.wav")
    sequence._add_row(SequenceEvent(5004, 7, "weapons/here.wav"))
    events = sequence.values()["events"]
    assert any(e.option == "weapons/picked.wav" for e in events)
    assert any(e.frame == 7 and e.option == "weapons/here.wav" for e in events)
