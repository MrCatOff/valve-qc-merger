"""Sounds that could be one file: finding them, sharing one, applying it."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pytest

from valve_qc_merger.project import Build, Project
from valve_qc_merger.project import sounds as library
from valve_qc_merger.sound.similar import analyse, role
from valve_qc_merger.sound.wav import _resample, write_wav

_ANACONDA = Path(__file__).parent / "examples" / "v_anaconda"


def _click(seconds: float = 0.4, freq: float = 900.0, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.linspace(0, seconds, int(seconds * 22050), endpoint=False)
    return (np.sin(2 * np.pi * freq * t) * np.exp(-t * 20)
            + 0.2 * rng.standard_normal(len(t)) * np.exp(-t * 30)) * 0.8


def _write(path: Path, samples: np.ndarray, rate: int = 22050) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(write_wav(samples if samples.ndim == 2 else samples[:, None], rate))
    return path


def test_levels_and_stock(tmp_path: Path) -> None:
    click = _click()
    up = _resample(click[:, None], 22050, 44100)[:, 0]
    quiet = np.concatenate([np.zeros(1600), up * 0.5, np.zeros(600)])
    sounds = {
        "a/m4_clipin.wav": _write(tmp_path / "1.wav", click),
        "b/ak_clipin.wav": _write(tmp_path / "2.wav", click),  # identical bytes
        "c/sg_clipin.wav": _write(tmp_path / "3.wav", np.column_stack([quiet, quiet]),
                                  44100),  # stereo, 44.1 kHz, quieter, padded
        "d/aug_clipin.wav": _write(tmp_path / "4.wav", _click(0.42, 950, 1)),  # similar
        "e/knife_hit1.wav": _write(tmp_path / "5.wav", _click(0.42, 950, 7)),  # other job
        "f/boom-1.wav": _write(tmp_path / "6.wav", np.random.default_rng(2)
                               .standard_normal(30000) * np.exp(-np.linspace(0, 5, 30000))),
    }
    groups = analyse(sounds, users={"c/sg_clipin.wav": 3})
    same = next(g for g in groups if g.level == "same")
    assert same.members == ["a/m4_clipin.wav", "b/ak_clipin.wav", "c/sg_clipin.wav"]
    assert same.keeper == "c/sg_clipin.wav" and same.saves == 2  # the most played kept
    similar = next(g for g in groups if g.level == "similar")
    assert set(similar.members) == {"c/sg_clipin.wav", "d/aug_clipin.wav"}  # via the keeper
    assert not any("e/knife_hit1.wav" in g.members for g in groups if g.level == "similar")
    assert not any("f/boom-1.wav" in g.members for g in groups)
    stock = analyse(sounds, {"weapons/clipin1.wav": _write(tmp_path / "s.wav", click * 0.9)})
    with_stock = next(g for g in stock if g.level == "same")
    assert with_stock.keeper == "weapons/clipin1.wav" and with_stock.saves == 3
    assert analyse(sounds, similar_threshold=2.0)[-1].level != "similar"
    assert (role("x/m4_clipin.wav"), role("x/knife_hit1.wav"), role("x/ak47-1.wav")) == \
        ("clipin", "hit", "shot")


def test_aliases_rewrite_events_and_counts(tmp_path: Path) -> None:
    qc = ('$sequence "reload" "a" {\n { event 5004 1 "weapons/m4_clipin.wav" }\n'
          ' { event 5004 9 "Weapons\\AK_clipin.wav" }\n}\n'
          '$sequence "shoot1" "b" {\n { event 5004 0 "weapons/ak47-1.wav" }\n}\n')
    aliases = {"weapons/m4_clipin.wav": "weapons/ak_clipin.wav",
               "weapons/ak_clipin.wav": "weapons/sg_clipin.wav"}  # a chain
    text = library.apply_aliases(qc, aliases)
    assert text.count('"weapons/sg_clipin.wav"') == 2
    assert library.resolve_alias(aliases, "WEAPONS/M4_CLIPIN.WAV") == "weapons/sg_clipin.wav"
    assert library.apply_aliases(qc, {"a.wav": "b.wav", "b.wav": "a.wav"}) == qc  # no loop
    project = Project.create(tmp_path / "pack")
    project.settings.sound_aliases = aliases
    kinds = library.project_sound_kinds(project, [qc])
    # one clip sound left; the stock shot costs nothing (ReGameDLL has it)
    assert kinds == {"weapons/sg_clipin.wav": "generic"}


def test_builds_stage_the_shared_sounds(tmp_path: Path) -> None:
    from valve_qc_merger.services.base import Reporter
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_ANACONDA)  # plays weapons/ana_foley1..5.wav
    project.add_build(Build("view", "merge-v"))
    project.settings.sound_aliases = {"weapons/ana_foley2.wav": "weapons/ana_foley1.wav"}
    work = tmp_path / "work"

    class Quiet(Reporter):
        def log(self, message: str) -> None:
            pass

    staged = project._stage("view", work, Quiet())
    assert staged is not None
    qc = next((work / "input" / "v_anaconda").glob("*.qc")).read_text(encoding="latin-1")
    assert "ana_foley2.wav" not in qc and qc.count("ana_foley1.wav") >= 4
    original = next(project.asset_dir("v_anaconda").glob("*.qc")).read_text("latin-1")
    assert "ana_foley2.wav" in original  # the asset itself is untouched
    shutil.rmtree(work)


QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_similar_sounds_dialog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from valve_qc_merger.studio import audio
    from valve_qc_merger.studio.similar_sounds import SimilarSoundsDialog
    played: list[str] = []
    monkeypatch.setattr(audio, "play", lambda path: played.append(Path(path).name) or True)
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_ANACONDA)
    base = library.sounds_dir(project) / "weapons"
    click = _click()
    _write(base / "ana_foley2.wav", click)  # reload: played by the model
    _write(base / "ana_foley3.wav", click)  # reload too, the same bytes
    _write(base / "zm_extra.wav", click)  # no model plays it: a plugin's
    dialog = SimilarSoundsDialog(project)
    try:
        (group,) = dialog.groups
        assert group.level == "exact" and len(group.members) == 3
        from PySide6.QtCore import Qt
        node = dialog.tree.topLevelItem(0)
        assert node.checkState(0) == Qt.CheckState.Checked
        plugin_row = next(node.child(i) for i in range(node.childCount())
                          if node.child(i).text(0) == "weapons/zm_extra.wav")
        assert plugin_row.text(2) == "no model — your plugin names it"
        files, _sounds, generic = dialog.savings()
        assert files == 1 and generic == 1  # one of the two played files goes
        dialog._play(plugin_row, 0)
        assert played == ["zm_extra.wav"]
        assert dialog.apply() == 1
        assert len(project.settings.sound_aliases) == 1
        assert dialog.shared_tree.topLevelItemCount() == 1
        # left: the kept sound and the plugin's copy — shown, nothing to tick
        (left,) = dialog.groups
        assert "weapons/zm_extra.wav" in left.members
        assert dialog.tree.topLevelItem(0).checkState(0) == Qt.CheckState.Unchecked
        dialog.shared_tree.topLevelItem(0).setSelected(True)
        dialog.unshare()
        assert project.settings.sound_aliases == {}
        assert len(dialog.groups[0].members) == 3
    finally:
        dialog.close()
