"""Studio M3: scene posing/skinning (numpy, Qt-free) and an offscreen GL render."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest

from valve_qc_merger.merge_view.discovery import load_model
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.studio.scene import build_scene, euler_to_quat, quat_to_matrix
from valve_qc_merger.transform import euler_to_matrix

_MINI = Path("tests/examples/mdl/src")


def test_quaternions_follow_the_goldsource_euler_convention() -> None:
    rng = np.random.default_rng(7)
    eulers = rng.uniform(-3.1, 3.1, (64, 3))
    got = quat_to_matrix(euler_to_quat(eulers))
    want = np.array([euler_to_matrix(Vector3(*e)) for e in eulers])
    assert np.abs(got - want).max() < 1e-12


def test_bind_pose_skin_returns_the_reference_vertices() -> None:
    model = load_model(_MINI)
    scene = build_scene(_MINI, model)
    rot, trans = scene.world(scene.bind_positions, scene.bind_quats)
    for batch in scene.batches:
        pos, _nrm = scene.skin(batch, rot, trans)
        source = model.meshes[batch.stem]
        want = np.array([tuple(v.position) for t in source.triangles
                         if t.material == batch.material for v in t.vertices])
        assert np.abs(pos - want).max() < 1e-9


def test_frames_interpolate_and_clamp() -> None:
    scene = build_scene(_MINI)
    shoot = [s.name for s in scene.sequences].index("shoot")  # root x = 1 + t
    root = scene.bone_names.index("root")
    pos, _q = scene.local_pose(shoot, 0.5)
    assert pos[root][0] == pytest.approx(1.5)
    pos, _q = scene.local_pose(shoot, 99.0)  # not looping: clamps to the end
    assert pos[root][0] == pytest.approx(3.0)
    idle = [s.name for s in scene.sequences].index("idle")  # looping, 2 frames
    a, _ = scene.local_pose(idle, 0.25)
    b, _ = scene.local_pose(idle, 2.25)
    assert np.allclose(a, b)


def test_bodygroup_choice_and_blank() -> None:
    scene = build_scene(_MINI)
    assert scene.groups == {"body": ["body"], "gun": ["blank", "gun"]}
    assert {b.stem for b in scene.visible_batches({})} == {"body"}
    assert {b.stem for b in scene.visible_batches({"gun": 1})} == {"body", "gun"}
    glass = next(b for b in scene.batches if b.material == "glass.bmp")
    assert glass.render_mode == "masked"
    lo, hi = scene.bounds()
    assert (hi - lo).max() > 1.0


_RENDER = textwrap.dedent("""
    import sys
    from pathlib import Path
    from PySide6.QtGui import QGuiApplication, QSurfaceFormat
    from valve_qc_merger.studio.renderer import ViewState, gl_format, render_offscreen
    from valve_qc_merger.studio.scene import build_scene
    QSurfaceFormat.setDefaultFormat(gl_format())
    app = QGuiApplication(sys.argv)
    scene = build_scene(Path(sys.argv[1]))
    state = ViewState(bodygroups={"gun": 1})
    state.camera.frame(*scene.bounds())
    try:
        image = render_offscreen(scene, state, 160, 120)
    except RuntimeError as exc:
        print("NO_GL", exc)
        raise SystemExit(0)
    image.save(sys.argv[2])
    print("OK")
""")


def test_offscreen_render_draws_the_model(tmp_path: Path) -> None:
    pytest.importorskip("PySide6.QtOpenGL")
    script = tmp_path / "render.py"
    script.write_text(_RENDER)
    out = tmp_path / "frame.png"
    # the GUI tests force Qt's GL-less "offscreen" platform for this process;
    # the render needs the native one
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    run = subprocess.run([sys.executable, str(script), str(_MINI), str(out)],
                         capture_output=True, text=True, timeout=120, env=env)
    no_display = run.returncode != 0 and ("platform" in run.stderr.lower()
                                          or os.environ.get("CI"))
    if "NO_GL" in run.stdout or no_display:
        pytest.skip(f"no OpenGL here: {run.stdout.strip() or run.stderr.strip()[:200]}")
    assert run.returncode == 0, run.stderr
    from PySide6.QtGui import QImage
    image = QImage(str(out))
    background = image.pixelColor(0, 0)
    drawn = sum(image.pixelColor(x, y) != background
                for x in range(0, 160, 4) for y in range(0, 120, 4))
    assert drawn > 40  # the textured quads cover a good part of the frame
