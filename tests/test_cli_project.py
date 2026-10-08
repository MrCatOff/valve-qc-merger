"""``valve-qc-merger project``: a Studio project without the window."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from tests.test_merge_zhands import _write_model
from valve_qc_merger.cli import main
from valve_qc_merger.project import Build, Project


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    _write_model(src, "v_alpha_knife", grenade=False, hand_root="Bone01")
    _write_model(src, "v_alpha_grenade", grenade=True, hand_root="Bone01")
    project = Project.create(tmp_path / "pack")
    project.add_build(Build("zh", "merge-zhands"))
    assert main(["project", "import", str(project.root), str(src), "--decompiled"]) == 0
    return project.root


def test_info_build_and_unknown_names(project: Path, capsys: pytest.CaptureFixture[str]
                                      ) -> None:
    assert len(Project.open(project).assets) == 2
    assert main(["project", "info", str(project)]) == 0
    assert "not run" in capsys.readouterr().out
    assert main(["project", "build", str(project)]) == 0
    main(["project", "info", str(project)])
    assert "run: 1 QC, 0 compiled" in capsys.readouterr().out
    assert main(["project", "build", str(project), "nope"]) == 3
    assert "no build nope" in capsys.readouterr().out
    assert main(["project", "deploy", str(project)]) == 3  # no game folder, not compiled


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake compiler")
def test_build_compile_deploy_with_overrides(project: Path, tmp_path: Path) -> None:
    fake = tmp_path / "studiomdl"
    fake.write_text("#!/bin/sh\nprintf 'IDST' > v_zhands.mdl\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    game = tmp_path / "game" / "cstrike"
    assert main(["project", "build", str(project), "zh", "--deploy", "--studiomdl", str(fake),
                 "--game-dir", str(game)]) == 0
    assert (game / "models" / "v_zhands.mdl").read_bytes() == b"IDST"
    assert (project / "builds" / "zh" / "cstrike" / "models" / "v_zhands.mdl").exists()
    assert Project.open(project).settings.studiomdl is None  # overrides are not saved
    out = tmp_path / "pkg"
    assert main(["project", "package", str(project), "--out", str(out)]) == 0
    assert (out / "cstrike" / "models" / "v_zhands.mdl").exists()
