"""Deploy: the build's server files (builds/<name>/cstrike) into the game folder."""

from __future__ import annotations

import re
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
    assert (project.server_tree("zh") / "models" / "v_zhands.mdl").exists()


def _play_sounds(project: Project, *sounds: str) -> None:
    """Give the built QC's idle sequence client sound events."""
    import json
    record = json.loads((project.build_dir("zh") / "last_run.json").read_text())
    qc = project.root / record["outputs"][0]
    events = " ".join(f'{{ event 5004 1 "{s}" }}' for s in sounds)
    qc.write_text(re.sub(r"(\$sequence \S+ \{)", rf"\1 {events}", qc.read_text(), count=1))


def test_server_tree_holds_models_manifest_and_played_sounds(project: Project,
                                                              tmp_path: Path) -> None:
    from valve_qc_merger.project import sounds as library
    build = project.builds["zh"]
    build.deploy_dir = "models/my/path"
    project.update_build(build)
    with pytest.raises(ProjectError, match="not compiled"):
        project.stage_server_tree("zh")
    _play_sounds(project, "weapons/zh/slash.wav", "weapons/knife_hit1.wav",
                 "weapons/zh/missing.wav")
    wav = library.sounds_dir(project) / "weapons" / "zh" / "slash.wav"
    wav.parent.mkdir(parents=True)
    wav.write_bytes(b"RIFF")
    mdl = _fake_compile(project)
    reporter = CollectingReporter()
    tree = project.stage_server_tree("zh", reporter)
    assert tree == project.build_dir("zh") / "cstrike"
    files = sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file())
    assert files == [f"models/my/path/{mdl.name}", f"models/my/path/{mdl.stem}T.mdl",
                     "models/my/path/v_zhands_models.ini",
                     "sound/weapons/zh/slash.wav"]  # the stock knife_hit1 is the game's
    assert any("weapons/zh/missing.wav" in line for line in reporter.lines)

    # Deploy copies the tree as it is; a re-run makes it stale (gone)
    project.settings.game_dir = str(tmp_path / "game" / "cstrike")
    assert project.deploy_build("zh", CollectingReporter()).ok
    game = tmp_path / "game" / "cstrike"
    assert (game / "sound/weapons/zh/slash.wav").read_bytes() == b"RIFF"
    assert (game / "models/my/path" / mdl.name).exists()
    assert project.run_build("zh", CollectingReporter()).ok
    assert not tree.exists()


def test_deploy_makes_a_missing_server_tree(project: Project, tmp_path: Path) -> None:
    project.settings.game_dir = str(tmp_path / "cstrike")
    mdl = _fake_compile(project)
    assert not project.server_tree("zh").exists()
    names = {d.relative_to(tmp_path / "cstrike").as_posix()
             for _s, d in project.deploy_pairs("zh")}
    assert names == {f"models/{mdl.name}", f"models/{mdl.stem}T.mdl",
                     "models/v_zhands_models.ini"}
    assert project.server_tree("zh").is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake compiler")
def test_compile_skips_unchanged_models_also_across_a_run(project: Project,
                                                          tmp_path: Path) -> None:
    count = tmp_path / "count"
    fake = tmp_path / "studiomdl"
    # run without a QC (the limits check) it does nothing
    fake.write_text(f"#!/bin/sh\n[ -n \"$1\" ] || exit 1\necho x >> {count}\n"
                    "printf 'IDST' > v_zhands.mdl\nprintf 'T' > v_zhandsT.mdl\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    project.settings.studiomdl = str(fake)

    def runs() -> int:
        return len(count.read_text().split()) if count.exists() else 0

    assert project.compile_build("zh", CollectingReporter()).ok and runs() == 1
    reporter = CollectingReporter()
    assert project.compile_build("zh", reporter).ok and runs() == 1
    assert any("up to date: v_zhands.mdl" in line for line in reporter.lines)
    # a run rewrites the same sources: the stashed model comes back
    assert project.run_build("zh", CollectingReporter()).ok
    mdl = compiled_model_path(project.build_dir("zh") / "output" / "v_zhands.qc")
    assert not mdl.exists()
    assert project.compile_build("zh", CollectingReporter()).ok and runs() == 1
    assert mdl.exists() and mdl.with_name("v_zhandsT.mdl").exists()
    # a changed source compiles again; force compiles anyway
    smd = next((project.build_dir("zh") / "output").rglob("*.smd"))
    smd.write_text(smd.read_text() + "\n")
    assert project.compile_build("zh", CollectingReporter()).ok and runs() == 2
    assert project.compile_build("zh", CollectingReporter(), force=True).ok and runs() == 3
