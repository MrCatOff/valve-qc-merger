"""The project's sound library and sound events in the studio."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pytest

from valve_qc_merger.project import Project, sounds
from valve_qc_merger.sound.wav import read_wav, write_wav

_ANACONDA = Path(__file__).parent / "examples" / "v_anaconda"


def _stereo_48k(path: Path) -> Path:
    t = np.arange(4800) / 48000
    tone = np.column_stack([np.sin(2 * np.pi * 440 * t), np.sin(2 * np.pi * 330 * t)]) * 0.4
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(write_wav(tone, 48000))
    return path


@pytest.fixture
def project(tmp_path: Path) -> Project:
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_ANACONDA)  # plays weapons/ana_foley1..5.wav
    return project


def test_import_keeps_the_path_after_sound(project: Project, tmp_path: Path) -> None:
    game = tmp_path / "server" / "cstrike" / "sound" / "weapons"
    _stereo_48k(game / "ana_foley1.wav")
    loose = _stereo_48k(tmp_path / "loose" / "Boom.WAV")
    folder = tmp_path / "pack_sounds" / "zombie"
    _stereo_48k(folder / "hit" / "claw.wav")
    names = sounds.import_sounds(project, [game / "ana_foley1.wav", loose, folder])
    assert names == ["weapons/ana_foley1.wav", "Boom.WAV", "zombie/hit/claw.wav"]
    assert sounds.list_sounds(project) == ["Boom.WAV", "weapons/ana_foley1.wav",
                                           "zombie/hit/claw.wav"]
    assert sounds.sound_users(project)["weapons/ana_foley1.wav"] == ["v_anaconda"]


def test_fix_and_undo(project: Project, tmp_path: Path) -> None:
    (name,) = sounds.import_sounds(project, [_stereo_48k(tmp_path / "sound" / "a.wav")])
    sounds.fix_sound(project, name)
    fixed = read_wav(sounds.sound_path(project, name))
    assert (fixed.channels, fixed.bits, fixed.rate) == (1, 16, 44100)
    assert sounds.can_undo_fix(project, name)
    sounds.undo_fix(project, name)
    assert read_wav(sounds.sound_path(project, name)).channels == 2
    assert not sounds.can_undo_fix(project, name)
    sounds.remove_sound(project, name)
    assert sounds.list_sounds(project) == []


def test_sequence_sounds_and_resolve(project: Project, tmp_path: Path) -> None:
    from valve_qc_merger.project.qc_edit import qc_file
    text = qc_file(project.asset_dir("v_anaconda")).read_text(encoding="latin-1")
    events = sounds.sequence_sounds(text)
    assert events["reload"] == [(0, "weapons/ana_foley2.wav"), (17, "weapons/ana_foley4.wav"),
                                (39, "weapons/ana_foley3.wav")]
    assert sounds.resolve(project, "weapons/ana_foley2.wav") is None
    mod = tmp_path / "cstrike"
    _stereo_48k(mod / "sound" / "Weapons" / "ANA_foley2.wav")
    project.settings.game_dir = str(mod)
    found = sounds.resolve(project, "weapons/ana_foley2.wav")  # case-insensitive
    assert found is not None and found.samefile(mod / "sound" / "Weapons" / "ANA_foley2.wav")


QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture
def window(tmp_path: Path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from PySide6.QtCore import QSettings

    from valve_qc_merger.studio.main_window import MainWindow
    win = MainWindow(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    yield win
    win.close()


def test_sounds_in_explorer_and_panel(window, project: Project, tmp_path: Path) -> None:
    sounds.import_sounds(project, [_stereo_48k(tmp_path / "sound" / "weapons"
                                               / "ana_foley1.wav")])
    window.set_project(project)
    assert window.explorer.select("sound", "weapons/ana_foley1.wav")
    panel = window.sound_panel
    assert window.right.currentWidget() is panel
    assert panel.values["channels"].text() == "2 (stereo)"
    assert panel.values["rate"].text() == "48000 Hz"
    assert "▲" in panel.verdict.text()
    links = [w.text() for w in panel.findChildren(QtWidgets.QPushButton)
             if w.property("role") == "link"]
    assert links == ["v_anaconda"]


def test_sound_events_mark_the_timeline_and_play(window, project: Project, tmp_path: Path,
                                                 monkeypatch) -> None:
    from valve_qc_merger.studio import audio
    played: list[str] = []
    monkeypatch.setattr(audio, "play", lambda path: played.append(Path(path).name) or True)
    for n in range(1, 6):
        _stereo_48k(sounds.sounds_dir(project) / "weapons" / f"ana_foley{n}.wav")
    window.set_project(project)
    window.explorer.select("asset", "v_anaconda")
    panel = window.viewport
    names = [panel.sequence_box.itemText(i) for i in range(panel.sequence_box.count())]
    panel.sequence_box.setCurrentIndex(next(i for i, n in enumerate(names) if "reload" in n))
    assert panel.slider.markers == [0, 17, 39]
    panel.sound_box.setChecked(True)
    panel._play_events(10.0, 20.0, False)
    assert played == ["ana_foley4.wav"]
    played.clear()
    panel._play_events(38.0, 1.0, True)  # wrapped around the loop
    assert played == ["ana_foley2.wav", "ana_foley3.wav"]
    played.clear()
    panel.sound_box.setChecked(False)
    panel._play_events(10.0, 20.0, False)
    assert played == []
    shutil.rmtree(sounds.sounds_dir(project))
