"""Package versions: changed models get new names, update/ holds the changes."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np

from valve_qc_merger.project import Build, Project
from valve_qc_merger.server import versions
from valve_qc_merger.server.package import export_package
from valve_qc_merger.sound.wav import write_wav


def _model(seqgroups: list[str] | None = None, payload: bytes = b"") -> bytes:
    """A studio header with sequence groups at offset 200."""
    groups = seqgroups or []
    data = bytearray(b"IDST" + b"\0" * 196)
    struct.pack_into("<2i", data, 172, len(groups), 200)
    for name in groups:
        data += b"label".ljust(32, b"\0") + name.encode().ljust(64, b"\0") + b"\0" * 8
    return bytes(data) + payload


def test_names_groups_and_seqgroup_patch() -> None:
    assert versions.versioned("models/v_ak47.mdl", 2) == "models/v_ak47_v2.mdl"
    assert versions.versioned("models/v_ak47_v2.mdl", 3) == "models/v_ak47_v3.mdl"
    groups = versions.groups_of(["models/v_a.mdl", "models/v_aT.mdl", "models/v_a01.mdl",
                                 "models/v_b.mdl", "sprites/x.spr"])
    assert groups == {"models/v_a.mdl": ["models/v_a.mdl", "models/v_a01.mdl",
                                         "models/v_aT.mdl"],
                      "models/v_b.mdl": ["models/v_b.mdl"], "sprites/x.spr": ["sprites/x.spr"]}
    data = versions.patch_seqgroups(_model(["", "models\\v_a01.mdl"]), "v_a", "v_a_v2")
    names = [data[200 + i * 104 + 32:200 + i * 104 + 96].split(b"\0")[0] for i in range(2)]
    assert names == [b"", b"models/v_a_v201.mdl"]


def _project(tmp_path: Path) -> Project:
    project = Project.create(tmp_path / "pack")
    project.add_build(Build("view", "merge-v", options={"name": "v_pack"}))
    output = project.build_dir("view") / "output"
    output.mkdir(parents=True)
    (output / "v_pack.qc").write_text('$modelname "v_pack.mdl"\n$sequence "a" {\n "a"\n'
                                      ' { event 5004 1 "weapons/pack.wav" }\n}\n',
                                      encoding="latin-1")
    (output / "v_pack.mdl").write_bytes(_model(["", "models/v_pack01.mdl"], b"one"))
    (output / "v_pack01.mdl").write_bytes(b"IDSQ anims")
    (output / "v_packT.mdl").write_bytes(b"IDST textures")
    (output / "models.ini").write_text("[v_ak47]\nmodel = v_pack.mdl\npev_body = 0\n",
                                       encoding="utf-8")
    (project.build_dir("view") / "last_run.json").write_text(json.dumps(
        {"outputs": ["builds/view/output/v_pack.qc"]}), encoding="utf-8")
    sound = project.root / "sounds" / "weapons" / "pack.wav"
    sound.parent.mkdir(parents=True)
    sound.write_bytes(write_wav(np.zeros((100, 1)), 22050))
    return project


def test_exports_rename_what_changed(tmp_path: Path) -> None:
    project = _project(tmp_path)
    out = tmp_path / "out"
    first = export_package(project, out)
    assert first.versions.first and first.models == ["models/v_pack.mdl"]
    assert "the first one" in (out / "package_report.txt").read_text(encoding="utf-8")

    output = project.build_dir("view") / "output"
    (output / "v_pack.mdl").write_bytes(_model(["", "models/v_pack01.mdl"], b"two"))
    second = export_package(project, out)
    mod = out / "cstrike"
    assert second.versions.renamed == {"models/v_pack.mdl": "models/v_pack_v2.mdl"}
    assert second.models == ["models/v_pack_v2.mdl"]
    for name in ("v_pack_v2.mdl", "v_pack_v2T.mdl", "v_pack_v201.mdl"):
        assert (mod / "models" / name).is_file()
    assert not (mod / "models" / "v_pack.mdl").exists()  # the tree is rebuilt
    data = (mod / "models" / "v_pack_v2.mdl").read_bytes()
    assert b"models/v_pack_v201.mdl" in data
    inc = (out / "amxx" / "vqm_resources.inc").read_text(encoding="utf-8")
    assert '#define VQM_V_AK47_MODEL "models/v_pack_v2.mdl"' in inc
    assert "model = v_pack_v2.mdl" in (mod / "models" / "v_pack_models.ini").read_text(
        encoding="utf-8")
    update = out / "update" / "cstrike" / "models"
    assert sorted(p.name for p in update.iterdir()) == ["v_pack_v2.mdl", "v_pack_v201.mdl",
                                                       "v_pack_v2T.mdl"]
    removed = (out / "update" / "removed.txt").read_text(encoding="utf-8")
    assert "models/v_pack.mdl" in removed and "models/v_packT.mdl" in removed
    assert "models/v_pack_v2.mdl" in (out / "rechecker" / "resources.ini").read_text(
        encoding="utf-8")

    sound = project.root / "sounds" / "weapons" / "pack.wav"
    sound.write_bytes(write_wav(np.ones((100, 1)) * 0.1, 22050))
    third = export_package(project, out)
    assert not third.versions.renamed and third.models == ["models/v_pack_v2.mdl"]
    assert third.versions.in_place == ["sound/weapons/pack.wav"]
    assert not third.versions.removed and third.versions.export == 3
    report = (out / "package_report.txt").read_text(encoding="utf-8")
    assert "changed in place" in report and "sound/weapons/pack.wav" in report
    assert [p.name for p in (out / "update" / "cstrike" / "sound" / "weapons").iterdir()] \
        == ["pack.wav"]
    assert not (out / "update" / "cstrike" / "models").exists()
