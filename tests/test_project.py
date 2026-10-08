"""Studio projects (M0): project.toml round trip, import, builds, compile."""

from __future__ import annotations

import json
import stat
import sys
import threading
import tomllib
from pathlib import Path

import pytest

from tests.test_merge_zhands import _write_model
from valve_qc_merger.project import Build, Project, ProjectError, classify
from valve_qc_merger.project.toml_write import dumps
from valve_qc_merger.services.base import (
    CallbackReporter,
    Cancelled,
    CollectingReporter,
)
from valve_qc_merger.services.compile import CompileOptions, run_compile

_WORLD = Path("tests/examples/world")


def test_toml_writer_round_trips_project_shapes() -> None:
    data = {
        "project": {"name": "Pack \"one\"", "format": 1},
        "settings": {"studiomdl": "C:\\tools\\studiomdl.exe", "hlam": None},
        "assets": [{"name": "v_a", "kind": "v", "path": "assets/v/v_a"},
                   {"name": "w_b", "kind": "w", "path": "assets/w/w_b"}],
        "builds": [{"name": "pistols", "kind": "merge-v", "assets": [],
                    "retarget": True,
                    "options": {"shared_hands": True, "texture_budget": 80,
                                "exclude": ["v_x"], "ratio": 0.5}}],
    }
    loaded = tomllib.loads(dumps(data))
    assert loaded["settings"] == {"studiomdl": "C:\\tools\\studiomdl.exe"}
    assert loaded["project"]["name"] == 'Pack "one"'
    assert loaded["builds"][0]["options"]["exclude"] == ["v_x"]
    assert loaded["assets"][1]["path"] == "assets/w/w_b"


def test_classify_by_prefix() -> None:
    assert [classify(n) for n in ("v_ak47", "P_deagle", "w_awp", "arctic")] == \
        ["v", "p", "w", "player"]


def test_create_open_and_import(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack", "Pack")
    with pytest.raises(ProjectError):
        Project.create(tmp_path / "pack")
    added = project.import_decompiled(_WORLD)  # a folder of model folders
    assert sorted(a.name for a in added) == sorted(p.name for p in _WORLD.iterdir())
    assert all(a.kind == "w" for a in added)
    assert (tmp_path / "pack" / "assets" / "w" / "w_luger").is_dir()
    with pytest.raises(ProjectError):
        project.import_decompiled(_WORLD / "w_luger")  # already imported
    project.settings.studiomdl = "C:\\tools\\studiomdl.exe"
    project.save()

    reopened = Project.open(tmp_path / "pack")
    assert reopened.name == "Pack"
    assert set(reopened.assets) == set(project.assets)
    assert reopened.settings.studiomdl == "C:\\tools\\studiomdl.exe"


def test_zombie_hands_are_recognised(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _write_model(src, "v_alpha_knife", grenade=False, hand_root="Bone01")
    _write_model(src, "v_alpha_grenade", grenade=True, hand_root="Bone01")
    _write_model(src, "v_usp", grenade=False, hand_root="Bone01")
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(src)
    kinds = {name: asset.kind for name, asset in project.assets.items()}
    assert kinds == {"v_alpha_knife": "zhands", "v_alpha_grenade": "zhands",
                     "v_usp": "v"}
    assert (tmp_path / "pack" / "assets" / "zhands" / "v_alpha_knife").is_dir()
    project.set_kind("v_usp", "zhands")
    assert project.assets["v_usp"].path == "assets/zhands/v_usp"


def test_build_validation(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    with pytest.raises(ProjectError):
        project.add_build(Build("x", "merge-q"))
    with pytest.raises(ValueError):
        project.add_build(Build("x", "merge-w", options={"no_such_flag": 1}))
    with pytest.raises(ProjectError):
        project.add_build(Build("x", "merge-w", retarget=True))


def test_run_world_build_records_last_run(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_WORLD)
    project.add_build(Build("world", "merge-w", options={"name": "w_all"}))
    reporter = CollectingReporter()
    result = project.run_build("world", reporter)
    assert result.ok, reporter.lines
    assert result.outputs == [tmp_path / "pack" / "builds" / "world" / "output" / "w_all.qc"]
    assert result.gates and all(g.passed for g in result.gates)
    record = json.loads((tmp_path / "pack" / "builds" / "world" / "last_run.json")
                        .read_text())
    assert record["exit_code"] == 0
    assert record["outputs"] == ["builds/world/output/w_all.qc"]
    assert reporter.events[-1][0] == reporter.events[-1][1]  # progress finished
    # the assets themselves are untouched (staged copies were sanitised)
    assert (tmp_path / "pack" / "assets" / "w" / "w_luger").is_dir()


def test_run_zhands_build(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _write_model(src, "v_alpha_knife", grenade=False, hand_root="Bone01")
    _write_model(src, "v_alpha_grenade", grenade=True, hand_root="Bone01")
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(src)
    project.add_build(Build("zombies", "merge-zhands"))
    result = project.run_build("zombies", CollectingReporter())
    assert result.ok
    assert result.manifest["v_alpha_grenade"]["pev_body"] == 1


def test_build_rejects_assets_of_another_kind(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_WORLD)
    project.add_build(Build("bad", "merge-p", assets=["w_luger"]))
    with pytest.raises(ProjectError):
        project.run_build("bad", CollectingReporter())


def test_cancel_stops_a_build(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_WORLD)
    project.add_build(Build("world", "merge-w"))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        project.run_build("world", CallbackReporter(cancel=cancel))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake compiler")
def test_compile_service_with_a_fake_studiomdl(tmp_path: Path) -> None:
    qc = tmp_path / "m" / "model.qc"
    qc.parent.mkdir()
    qc.write_bytes(b'$modelname "out.mdl"\r\n$body studio "sub\\\\mesh"\r\n')
    (qc.parent / "sub").mkdir()
    (qc.parent / "sub" / "mesh.smd").write_bytes(b"version 1\nnodes\nend\n")
    fake = tmp_path / "studiomdl"
    fake.write_text("#!/bin/sh\necho compiling $1\nprintf 'IDST' > out.mdl\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    reporter = CollectingReporter()
    result = run_compile(CompileOptions(qc=qc, studiomdl=fake), reporter)
    assert result.ok, reporter.lines
    assert result.outputs == [qc.parent / "out.mdl"]
    assert b"\r\n" not in qc.read_bytes() and b"sub//mesh" in qc.read_bytes()
    assert "    compiling model.qc" in reporter.lines

    fake.write_text("#!/bin/sh\necho ERROR: bad\n")  # exits 0, writes nothing
    assert not run_compile(CompileOptions(qc=qc, studiomdl=fake), reporter).ok


def test_remove_asset_deletes_read_only_folders(tmp_path: Path) -> None:
    """Remove from project must really delete the folder: read-only files and
    folders (a common Windows reason rmtree silently left them) included."""
    import os
    import stat

    from valve_qc_merger.project.model import remove_tree
    project = Project.create(tmp_path / "pack")
    (asset,) = project.import_decompiled(Path(__file__).parent / "examples" / "v_anaconda")
    folder = project.asset_dir(asset.name)
    locked = folder / "locked"
    locked.mkdir()
    (locked / "texture.bmp").write_bytes(b"BM")
    os.chmod(locked / "texture.bmp", stat.S_IREAD)
    os.chmod(locked, stat.S_IREAD | stat.S_IEXEC)  # files inside cannot be unlinked
    assert project.remove_asset(asset.name) == []
    assert not folder.exists()
    assert remove_tree(tmp_path / "never-existed")
