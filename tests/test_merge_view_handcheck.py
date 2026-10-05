"""--shared-hands only merges models that wear the same hands."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from valve_qc_merger.merge_view.discovery import load_model
from valve_qc_merger.merge_view.handcheck import (
    dir_wears_hands,
    group_by_hands,
    hand_shape,
    shape_distance,
)
from valve_qc_merger.project import Build, Project
from valve_qc_merger.resources import resource_path
from valve_qc_merger.retarget.config import DEFAULT_SHARED_HANDS_REFERENCE
from valve_qc_merger.services.base import CollectingReporter
from valve_qc_merger.services.merge_view import MergeViewOptions, run_merge_view
from valve_qc_merger.services.retarget import RetargetOptions, run_retarget

_ANACONDA = Path("tests/examples/v_anaconda")
_CSO = resource_path(Path(DEFAULT_SHARED_HANDS_REFERENCE))


@pytest.fixture(scope="module")
def models(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """raw anaconda + two retargets of it (different grips)."""
    root = tmp_path_factory.mktemp("hands")
    shutil.copytree(_ANACONDA, root / "v_raw")
    for name, offset in (("v_rt", []), ("v_rt_moved", [0.0, 0.0, 1.0])):
        assert run_retarget(RetargetOptions(weapon_dir=_ANACONDA, out=root / name,
                                            weapon_offset=offset),
                            CollectingReporter()).ok
    return root


def _hands(directory: Path) -> list:
    model = load_model(directory)
    return [hand_shape(model.meshes[s]) for s in model.bodygroups["hands"]]


def test_retargets_agree_bone_locally_raw_hands_do_not(models: Path) -> None:
    rt, moved = _hands(models / "v_rt"), _hands(models / "v_rt_moved")
    # different grips -> different bind, same hands in bone-local space
    assert shape_distance(rt[0], moved[0]) < 1e-3
    raw = [hand_shape(load_model(models / "v_raw").meshes["grafted_male"])]
    assert shape_distance(rt[0], raw[0]) > 1.0
    assert group_by_hands([("raw", raw), ("a", rt), ("b", moved)]) == [["a", "b"], ["raw"]]
    assert dir_wears_hands(models / "v_rt_moved", _CSO)
    assert not dir_wears_hands(models / "v_raw", _CSO)


def test_shared_hands_merge_rejects_foreign_hands(models: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = run_merge_view(MergeViewOptions(models_dir=models, out=out, shared_hands=True),
                            CollectingReporter())
    assert any("v_raw" in f and "other hands" in f for f in result.failures)
    (gate,) = [g for g in result.gates if g.check == "shared_hands"]
    assert gate.passed and "2 model(s)" in gate.detail and "1 rejected" in gate.detail
    assert [p.name for p in (out / "hands").iterdir()] == ["hands.smd"]  # ours, not raw's
    assert "v_raw" not in (out / "models.ini").read_text()


def test_build_retargets_only_what_needs_it(models: Path, tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(models / "v_raw")
    project.import_decompiled(models / "v_rt_moved")  # a CLI retarget output
    _result, derived = project.derive_asset("v_raw", "hands",
                                            {"weapon_offset": [0.0, 0.5, 0.0]})
    project.add_build(Build("pack", "merge-v", retarget=True))
    reporter = CollectingReporter()
    result = project.run_build("pack", reporter)
    log = "\n".join(reporter.lines)
    assert "retarget v_raw_hands: already on our hands" in log
    assert "retarget v_rt_moved: already on our hands" in log
    # v_raw is the same weapon as v_raw_hands: left out, not retargeted twice
    assert "v_raw: left out, v_raw_hands is the same weapon on our hands" in log
    assert "retarget: 0 converted, 0 reused, 2 already on our hands, 0 failed" in log
    record = json.loads((project.build_dir("pack") / "last_run.json").read_text())
    assert not any("the same weapon twice" in w for w in record["warnings"])
    assert result.gates and all(g.passed for g in result.gates)


def test_plan_build_predicts_without_merging(models: Path, tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    for name in ("v_raw", "v_rt", "v_rt_moved"):
        project.import_decompiled(models / name)
    project.add_build(Build("shared", "merge-v", options={"shared_hands": True,
                                                         "name": "v_pack"}))
    result = project.plan_build("shared", CollectingReporter())
    record = json.loads((project.build_dir("shared") / "plan.json").read_text())
    (part,) = record["parts"]
    assert part["part"] == "v_pack" and part["models"] == ["v_rt", "v_rt_moved"]
    assert part["pev_body"] == {"v_rt": 0, "v_rt_moved": 1}  # one hand variant
    assert any("v_raw" in f for f in record["failures"]) and not result.ok
    assert not (project.build_dir("shared") / "output").exists()  # nothing merged
    assert not (project.build_dir("shared") / "plan").exists()  # staging cleaned
    # the run agrees with the plan
    project.run_build("shared", CollectingReporter())
    ini = (project.build_dir("shared") / "output" / "models.ini").read_text()
    assert "[v_rt_moved]\npev_body = 1" in ini  # one part: no model line
    with pytest.raises(KeyError):
        project.plan_build("nope")


def test_build_saves_its_retargets_and_reuses_them(models: Path, tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(models / "v_raw")
    project.add_build(Build("listed", "merge-v", assets=["v_raw"], retarget=True))
    reporter = CollectingReporter()
    project.run_build("listed", reporter)
    assert "retarget v_raw: saved as v_raw_hands" in "\n".join(reporter.lines)
    made = project.assets["v_raw_hands"]
    assert made.derived["from"] == "v_raw" and made.derived["mode"] == "hands"
    assert project.hands_asset("v_raw") is made
    again = CollectingReporter()
    project.run_build("listed", again)
    log = "\n".join(again.lines)
    assert "v_raw_hands (made before with these settings) reused" in log
    assert "retarget: 0 converted, 1 reused" in log
    # a build over every asset takes the saved one in place of its source
    project.add_build(Build("all", "merge-v", retarget=True))
    third = CollectingReporter()
    project.run_build("all", third)
    assert "v_raw: left out, v_raw_hands is the same weapon on our hands" in \
        "\n".join(third.lines)
