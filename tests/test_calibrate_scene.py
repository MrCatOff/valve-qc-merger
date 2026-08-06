"""CalibScene skinning + penetration (retarget stage-2 core).

Needs numpy (the optional ``[calibrate]`` extra), so the whole module is skipped
when it is not installed. A tiny synthetic model — a weapon face in the z=0 plane
and a hand face 1.5u behind it, on one shared skeleton — makes the penetration
signal exact and checkable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("numpy")

from valve_qc_merger.calibrate.scene import CalibScene, discover, scene_from_dir  # noqa: E402

_NODES = [(0, "root", -1), (1, "weapon", 0), (2, "hand", 0)]


def _smd(path: Path, *, tris=None, anim_frames: int = 0) -> None:
    """Write a minimal SMD. ``tris`` = list of (bone, [(pos, normal) * 3], material)
    for a mesh; ``anim_frames`` > 0 writes that many all-zero skeleton frames and no
    triangles (an animation SMD). A mesh always carries one rest frame at time 0."""
    lines = ["version 1", "nodes"]
    lines += [f'{i} "{name}" {parent}' for i, name, parent in _NODES]
    lines.append("end")
    lines.append("skeleton")
    for t in range(max(anim_frames, 1)):
        lines.append(f"time {t}")
        lines += [f"{i} 0 0 0 0 0 0" for i, _, _ in _NODES]
    lines.append("end")
    if tris is not None:
        lines.append("triangles")
        for bone, verts, material in tris:
            lines.append(material)
            for (px, py, pz), (nx, ny, nz) in verts:
                lines.append(f"{bone} {px} {py} {pz} {nx} {ny} {nz} 0 0")
        lines.append("end")
    path.write_text("\n".join(lines) + "\n")


# weapon: one face in the z=0 plane, outward normal +z, bound to bone "weapon" (1)
_WEAPON_TRI = (1, [((0.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
                   ((2.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
                   ((0.0, 2.0, 0.0), (0.0, 0.0, 1.0))], "weapon_mat")
# hand: a face 1.5u BEHIND the weapon (z=-1.5), inside its footprint, bone "hand" (2)
_HAND_TRI = (2, [((0.4, 0.4, -1.5), (0.0, 0.0, -1.0)),
                 ((0.8, 0.4, -1.5), (0.0, 0.0, -1.0)),
                 ((0.4, 0.8, -1.5), (0.0, 0.0, -1.0))], "hand_mat")


def _model_dir(tmp_path: Path) -> Path:
    d = tmp_path / "v_test"
    (d / "anims").mkdir(parents=True)
    _smd(d / "v_test.smd", tris=[_WEAPON_TRI])
    _smd(d / "hands_male.smd", tris=[_HAND_TRI])
    _smd(d / "anims" / "idle.smd", anim_frames=1)
    return d


def test_discover_retarget_layout(tmp_path: Path) -> None:
    kind, mesh, hands, anims = discover(_model_dir(tmp_path))
    assert kind == "retarget"
    assert Path(mesh).name == "v_test.smd"
    assert [Path(h).name for h in hands] == ["hands_male.smd"]
    assert list(anims) == ["idle"]


def test_pose_skins_weapon_and_hand(tmp_path: Path) -> None:
    scene = scene_from_dir(_model_dir(tmp_path))
    s = scene.pose("idle", 0)
    assert len(s["weapon"][0]) == 3          # one weapon triangle
    assert len(s["hand_points"]) == 3        # three unique hand verts


def test_penetration_reads_the_gap(tmp_path: Path) -> None:
    scene = scene_from_dir(_model_dir(tmp_path))
    s = scene.pose("idle", 0)
    pen = scene.penetration(s["base_weapon"], s["hand_points"])
    assert pen["pen_max"] == pytest.approx(1.5, abs=1e-6)   # hand sits 1.5u inside
    assert pen["pen_count"] == 3


def test_weapon_offset_clears_penetration(tmp_path: Path) -> None:
    scene = scene_from_dir(_model_dir(tmp_path))
    s = scene.pose("idle", 0)
    # push the weapon 2u down (past the hand): every hand vert now sits in front.
    pen = scene.penetration(s["base_weapon"], s["hand_points"], offset=(0.0, 0.0, -2.0))
    assert pen["pen_count"] == 0
    assert pen["pen_max"] == 0.0


def test_auto_seed_pushes_out_of_deep_intrusion(tmp_path: Path) -> None:
    scene = scene_from_dir(_model_dir(tmp_path))
    off, pen = scene.auto_seed("idle", 0, [0.0, 0.0, 0.0], stride=1)
    assert off[2] < 0.0                       # drove the weapon down, along -normal
    assert pen["pen_max"] < 0.8               # below the min_pen grip-contact floor


def test_from_combined_splits_by_material(tmp_path: Path) -> None:
    # hands + weapon in ONE mesh SMD, split by the material hint.
    d = tmp_path / "combined"
    (d / "anims").mkdir(parents=True)
    _smd(d / "v_test_ref.smd", tris=[_WEAPON_TRI, _HAND_TRI])
    _smd(d / "anims" / "idle.smd", anim_frames=1)
    scene = CalibScene.from_combined(str(d / "v_test_ref.smd"),
                                     {"idle": str(d / "anims" / "idle.smd")},
                                     hand_material_hint="hand")
    s = scene.pose("idle", 0)
    assert len(s["weapon"][0]) == 3
    pen = scene.penetration(s["base_weapon"], s["hand_points"])
    assert pen["pen_max"] == pytest.approx(1.5, abs=1e-6)
