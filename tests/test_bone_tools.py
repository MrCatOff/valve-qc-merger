"""Studio M5: bone-hierarchy edits on an asset, attachments, undo."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from valve_qc_merger.merge_view.discovery import load_model
from valve_qc_merger.merge_view.skeleton_ops import fk_worlds
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.project import Project
from valve_qc_merger.project.bones import (
    AttachmentSpec,
    BoneEditError,
    delete_bone,
    read_attachments,
    rename_bone,
    reparent,
    write_attachments,
)

_MINI = Path("tests/examples/mdl/src")


@pytest.fixture()
def asset(tmp_path: Path) -> Path:
    target = tmp_path / "mini"
    shutil.copytree(_MINI, target)
    return target


def _world(directory: Path, smd_name: str, bone: str, frame: int = 0) -> Vector3:
    model = load_model(directory)
    smd = model.anims.get(smd_name) or model.meshes[smd_name]
    index = next(n.index for n in smd.nodes if n.name == bone)
    return fk_worlds(smd, smd.frames[frame])[index].translation


def test_rename_updates_every_smd_and_the_qc(asset: Path) -> None:
    result = rename_bone(asset, "gun", "weapon")
    assert len(result.files) == 5  # 2 meshes + 2 anims + the QC
    model = load_model(asset)
    for smd in [*model.meshes.values(), *model.anims.values()]:
        assert [n.name for n in smd.nodes] == ["root", "weapon"]
    qc = (asset / "mini.qc").read_text()
    assert '$attachment 0 "weapon"' in qc and '$hbox 1 "weapon"' in qc
    assert '$controller 0 "weapon"' in qc
    with pytest.raises(BoneEditError):
        rename_bone(asset, "weapon", "root")  # clash
    with pytest.raises(BoneEditError):
        rename_bone(asset, "weapon", "x" * 40)  # studiomdl keeps 31


def test_reparent_keeps_world_poses_and_refuses_cycles(asset: Path) -> None:
    before = [_world(asset, "shoot", "gun", f) for f in range(3)]
    result = reparent(asset, "gun", None)  # make it a second root
    assert result.max_pose_deviation < 1e-9
    after = [_world(asset, "shoot", "gun", f) for f in range(3)]
    assert all(max(abs(a - b) for a, b in zip(x, y, strict=True)) < 1e-5
               for x, y in zip(before, after, strict=True))
    reparent(asset, "root", "gun")  # now gun is the parent of root: fine
    with pytest.raises(BoneEditError):
        reparent(asset, "gun", "root")  # root is gun's descendant now


def test_delete_moves_vertices_and_attachment_to_the_parent(asset: Path) -> None:
    attach_world = None
    model = load_model(asset)
    mesh = model.meshes["gun"]
    worlds = fk_worlds(mesh, mesh.frames[0])
    gun = next(n.index for n in mesh.nodes if n.name == "gun")
    attach_world = worlds[gun].transform_point(Vector3(1, 2, 3))

    result = delete_bone(asset, "gun")
    assert any("vertices moved" in w for w in result.warnings)
    model = load_model(asset)
    assert [n.name for n in model.meshes["gun"].nodes] == ["root"]
    (spec,) = read_attachments(asset)
    assert spec.bone == "root"
    mesh = model.meshes["gun"]
    point = fk_worlds(mesh, mesh.frames[0])[0].transform_point(Vector3(*spec.offset))
    assert max(abs(a - b) for a, b in zip(point, attach_world, strict=True)) < 1e-4
    with pytest.raises(BoneEditError):
        delete_bone(asset, "root")  # a root carrying vertices


def test_write_attachments(asset: Path) -> None:
    write_attachments(asset, [AttachmentSpec(1, "root", (0, -5, 0)),
                              AttachmentSpec(0, "gun", (1, 1, 1))])
    assert [(a.index, a.bone) for a in read_attachments(asset)] == [(0, "gun"), (1, "root")]
    load_model(asset)  # the QC still parses
    with pytest.raises(BoneEditError):
        write_attachments(asset, [AttachmentSpec(0, "nope", (0, 0, 0))])
    with pytest.raises(BoneEditError):
        write_attachments(asset, [AttachmentSpec(0, "gun", (0, 0, 0)),
                                  AttachmentSpec(0, "root", (0, 0, 0))])


def test_snapshot_and_undo(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(_MINI)
    directory = project.asset_dir("src")
    original = (directory / "mini.qc").read_text()
    assert not project.can_undo("src")
    for new in ("a", "b"):
        project.snapshot_asset("src")
        rename_bone(directory, "gun" if new == "a" else "a", new)
    assert project.undo_asset("src")
    assert '"a"' in (directory / "mini.qc").read_text()
    assert project.undo_asset("src")
    assert (directory / "mini.qc").read_text() == original
    assert not project.undo_asset("src")


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_bone_page_edits_through_the_window(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings, Qt

    from valve_qc_merger.studio.main_window import MainWindow
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    try:
        project = Project.create(tmp_path / "pack")
        project.import_decompiled(_MINI)
        window.set_project(project)
        assert window.explorer.select("asset", "src")
        page = window.inspector.bones_page
        item = page.tree.findItems("gun", Qt.MatchFlag.MatchExactly
                                   | Qt.MatchFlag.MatchRecursive)[0]
        page.tree.setCurrentItem(item)
        assert window.viewport.viewport.state.highlight_bone == 1
        window.reparent_bone("gun", None)
        assert window.jobs.wait(30_000)
        page = window.inspector.bones_page
        assert page.tree.topLevelItemCount() == 2  # gun is a root now
        assert page.undo_button.isEnabled()
        window.undo_asset_edit()
        assert window.inspector.bones_page.tree.topLevelItemCount() == 1
        specs = window.inspector.attachments_page.specs()
        assert [(s.index, s.bone) for s in specs] == [(0, "gun")]
    finally:
        window.close()
