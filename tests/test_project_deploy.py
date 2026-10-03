"""Deploy: compiled models + manifest into the game folder."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from tests.test_merge_zhands import _write_model
from valve_qc_merger.project import Build, Project, ProjectError
from valve_qc_merger.services.base import CollectingReporter
from valve_qc_merger.services.compile import compiled_model_path


@pytest.fixture()
def project(tmp_path: Path) -> Project:
    src = tmp_path / "src"
    _write_model(src, "v_alpha_knife", grenade=False, hand_root="Bone01")
    _write_model(src, "v_alpha_grenade", grenade=True, hand_root="Bone01")
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(src)
    project.add_build(Build("zh", "merge-zhands"))
    assert project.run_build("zh", CollectingReporter()).ok
    return project


def _fake_compile(project: Project) -> Path:
    import json
    record = json.loads((project.build_dir("zh") / "last_run.json").read_text())
    (qc,) = record["outputs"]
    mdl = compiled_model_path(project.root / qc)
    mdl.write_bytes(b"IDST")
    mdl.with_name(f"{mdl.stem}T.mdl").write_bytes(b"IDST")
    return mdl


def test_deploy_needs_game_folder_and_compiled_models(project: Project,
                                                      tmp_path: Path) -> None:
    with pytest.raises(ProjectError, match="game folder"):
        project.deploy_files("zh")
    project.settings.game_dir = str(tmp_path / "cstrike")
    with pytest.raises(ProjectError, match="not compiled"):
        project.deploy_files("zh")
    mdl = _fake_compile(project)
    pairs = {d.relative_to(tmp_path / "cstrike").as_posix() for _s, d in
             project.deploy_files("zh")}
    assert pairs == {f"models/{mdl.name}", f"models/{mdl.stem}T.mdl",
                     "models/v_zhands_models.ini"}  # models.ini -> <name>_models.ini
    result = project.deploy_build("zh", CollectingReporter())
    assert result.ok and (tmp_path / "cstrike" / "models" / mdl.name).read_bytes() == b"IDST"


def test_deploy_dir_and_manifest_prefix(project: Project, tmp_path: Path) -> None:
    project.settings.game_dir = str(tmp_path / "game")
    _fake_compile(project)
    build = project.builds["zh"]
    build.deploy_dir = "models/custom"
    build.options = {"name": "v_zombies"}
    project.update_build(build)
    names = sorted(d.relative_to(tmp_path / "game").as_posix()
                   for _s, d in project.deploy_files("zh"))
    assert names == ["models/custom/v_zhands.mdl", "models/custom/v_zhandsT.mdl",
                     "models/custom/v_zombies_models.ini"]
    reopened = Project.open(project.root)
    assert reopened.builds["zh"].deploy_dir == "models/custom"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake compiler")
def test_deploy_after_compile(project: Project, tmp_path: Path) -> None:
    fake = tmp_path / "studiomdl"
    fake.write_text("#!/bin/sh\nprintf 'IDST' > v_zhands.mdl\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    project.settings.studiomdl = str(fake)
    project.settings.game_dir = str(tmp_path / "cstrike")
    project.settings.deploy_after_compile = True
    project.save()
    assert Project.open(project.root).settings.deploy_after_compile
    reporter = CollectingReporter()
    assert project.compile_build("zh", reporter).ok, reporter.lines
    assert (tmp_path / "cstrike" / "models" / "v_zhands.mdl").exists()
