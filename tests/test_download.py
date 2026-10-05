"""Download size of the package and the ways to make it smaller."""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import numpy as np
import pytest

from valve_qc_merger.project import Build, Project
from valve_qc_merger.server import download
from valve_qc_merger.sound.wav import write_wav


def studio_model(textures: list[tuple[str, int, int]], used_refs: list[int]) -> bytes:
    """A studio model: textures (+ a 1-family skin table) and one bodypart
    with one model whose meshes use ``used_refs``."""
    data = bytearray(b"IDST" + struct.pack("<i", 10) + b"\0" * 236)
    tex_at, skin_at = 300, 300 + 80 * len(textures)
    body_at = skin_at + 2 * len(textures) + 2
    model_at, mesh_at = body_at + 76, body_at + 76 + 112
    fields = [0] * 21
    fields[11], fields[12] = len(textures), tex_at
    fields[14], fields[15], fields[16] = len(textures), 1, skin_at
    fields[17], fields[18] = 1, body_at
    struct.pack_into("<21i", data, 136, *fields)
    data += b"\0" * (mesh_at + 20 * len(used_refs) - len(data))
    for i, (name, w, h) in enumerate(textures):
        struct.pack_into("<64s3i", data, tex_at + i * 80, name.encode(), 0, w, h)
        struct.pack_into("<h", data, skin_at + i * 2, i)
    struct.pack_into("<3i", data, body_at + 64, 1, 1, model_at)
    struct.pack_into("<2i", data, model_at + 72, len(used_refs), mesh_at)
    for k, ref in enumerate(used_refs):
        struct.pack_into("<i", data, mesh_at + k * 20 + 8, ref)
    return bytes(data)


def test_model_suggestions(tmp_path: Path) -> None:
    path = tmp_path / "v_x.mdl"
    path.write_bytes(studio_model([("big.bmp", 1024, 1024), ("spare.bmp", 256, 128)], [0]))
    found = download.model_suggestions(path, "models/v_x.mdl")
    messages = [s.message for s in found]
    assert messages[0] == "1 texture(s) no skin uses: spare.bmp"
    assert found[0].saving == 256 * 128 + 768
    assert "big.bmp is 1024×1024" in messages[1] and found[1].saving == 1024 * 1024 - 512 * 512


def test_sound_and_duplicate_suggestions(tmp_path: Path) -> None:
    loud = tmp_path / "a.wav"
    loud.write_bytes(write_wav(np.zeros((4410, 2)), 44100))
    fine = tmp_path / "b.wav"
    fine.write_bytes(write_wav(np.zeros((100, 1)), 22050))
    copy = tmp_path / "c.wav"
    copy.write_bytes(fine.read_bytes())
    found = download.suggestions({"sound/a.wav": loud, "sound/b.wav": fine,
                                  "sound/c.wav": copy})
    assert found[0].path == "sound/a.wav" and "stereo → mono" in found[0].message
    assert "44100 Hz → 22050" in found[0].message
    assert found[0].saving == int(loud.stat().st_size * 0.75)
    assert found[1].kind == "duplicate" and found[1].message == "the same file as sound/c.wav"
    assert download.human(1536) == "1.5 KB" and download.duration(90 * 1024, 30 * 1024) == "3 s"


def _project(tmp_path: Path) -> Project:
    project = Project.create(tmp_path / "pack")
    project.add_build(Build("view", "merge-v", options={"name": "v_pack"}))
    output = project.build_dir("view") / "output"
    output.mkdir(parents=True)
    (output / "v_pack.qc").write_text('$modelname "v_pack.mdl"\n$sequence "a" {\n "a"\n'
                                      ' { event 5004 1 "weapons/pack.wav" }\n}\n',
                                      encoding="latin-1")
    (output / "v_pack.mdl").write_bytes(studio_model([("a.bmp", 64, 64), ("b.bmp", 64, 64)],
                                                     [0]))
    (project.build_dir("view") / "last_run.json").write_text(json.dumps(
        {"outputs": ["builds/view/output/v_pack.qc"]}), encoding="utf-8")
    sound = project.root / "sounds" / "weapons" / "pack.wav"
    sound.parent.mkdir(parents=True)
    sound.write_bytes(write_wav(np.zeros((2000, 1)), 44100))
    return project


def test_package_files(tmp_path: Path) -> None:
    files = download.package_files(_project(tmp_path))
    assert sorted(files) == ["models/v_pack.mdl", "sound/weapons/pack.wav"]


QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_download_tab(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from valve_qc_merger.studio.server_window import ServerWindow
    window = ServerWindow(_project(tmp_path))
    try:
        panel = window.download
        panel.scan()
        assert "A new player downloads" in panel.total_label.text()
        assert "(2 files)" in panel.total_label.text()
        assert panel.table.rowCount() == 2  # an unused texture, a 44.1 kHz sound
        assert "could save" in panel.saving_label.text()
        panel.view_box.setCurrentIndex(1)
        assert panel.table.horizontalHeaderItem(0).text() == "Size"
        assert panel.table.item(0, 1).text() == "sound/weapons/pack.wav"
        assert "no skin uses" in panel.report()
    finally:
        window.close()
