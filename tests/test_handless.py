"""View models whose hands are the model (or that have none): told on
import, merged by merge-v builds into a no-hands part."""

from __future__ import annotations

import configparser
import shutil
from pathlib import Path

from valve_qc_merger.project import Build, Project
from valve_qc_merger.project.handless import hands_are_the_model
from valve_qc_merger.services.base import CollectingReporter
from valve_qc_merger.services.decompile import DecompileOptions, run_decompile

_EXAMPLES = Path(__file__).parent / "examples"
_MINI = _EXAMPLES / "mdl" / "mini.mdl"


def _mini(tmp_path: Path, name: str) -> Path:
    staged = tmp_path / "decompiled"
    run_decompile(DecompileOptions(source=_MINI, out=staged), CollectingReporter())
    target = tmp_path / "src" / name
    shutil.copytree(staged / "mini", target)
    qc = next(target.glob("*.qc"))
    qc.rename(qc.with_name(f"{name}.qc"))
    return target


def test_detection(tmp_path: Path) -> None:
    assert hands_are_the_model(_EXAMPLES / "v_anaconda") is None  # held by hands
    gun = _mini(tmp_path, "v_floating")  # a gun, no hand bones, nothing named hand
    assert hands_are_the_model(gun) == "no hands"
    assert hands_are_the_model(gun, "/srv/models/zhh/claws/v_x.mdl") == "claws (named so)"
    qc = next(gun.glob("*.qc"))  # a group named "hand": hands the finder cannot see
    qc.write_text(qc.read_text().replace('$body "body"', '$body "hand"'))
    assert hands_are_the_model(gun) is None


def test_merge_v_build_puts_them_in_a_no_hands_part(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_EXAMPLES / "v_anaconda")
    project.import_decompiled(_mini(tmp_path, "v_floating"))
    assert project.assets["v_floating"].hands_model  # set on import
    assert not project.assets["v_anaconda"].hands_model
    project.add_build(Build("view", "merge-v", options={"name": "v_pack"}))
    reporter = CollectingReporter()
    result = project.run_build("view", reporter)
    assert result.ok, reporter.lines
    output = project.build_dir("view") / "output"
    assert output / "nohands" / "v_pack_nohands.qc" in result.outputs
    manifest = configparser.ConfigParser()
    manifest.read(output / "models.ini")
    assert manifest["v_floating"]["model"] == "v_pack_nohands.mdl"
    assert manifest["v_floating"]["hands"] == "model"
    assert manifest["v_floating"]["pev_body"] == "0"
    assert manifest["v_anaconda"]["model"] == "v_pack.mdl"
    assert Project.open(project.root).assets["v_floating"].hands_model  # saved


def test_import_makes_claws_zombie_hands(tmp_path: Path) -> None:
    claws = tmp_path / "server" / "models" / "zhh" / "claws"
    claws.mkdir(parents=True)
    shutil.copy(_MINI, claws / "v_knife_zombie.mdl")
    shutil.copy(_MINI, claws / "alien_claw.mdl")  # no v_ name: still a view model
    project = Project.create(tmp_path / "pack")
    project.import_models([tmp_path / "server" / "models"])
    assert project.assets["v_knife_zombie"].kind == "zhands"  # zombie hands, by the path
    assert project.assets["alien_claw"].kind == "zhands"
