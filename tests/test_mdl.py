"""Pure-Python .mdl reading / decompiling (studio M1).

Fixtures in tests/examples/mdl were compiled by stock HLSDK studiomdl from
tests/examples/mdl/src (QC + SMD + BMP), so every value can be checked
against its source: two bodygroups (one with a blank), a masked texture,
a looping sequence with an activity and a sound event, attachment, hitbox,
controller, and a ``$externaltextures`` twin (``mini_extT.mdl``).
"""

from __future__ import annotations

import math
from pathlib import Path

from valve_qc_merger.mdl.decompile import decompile_mdl
from valve_qc_merger.mdl.reader import MOTION_FLAGS, NF_MASKED, read_mdl
from valve_qc_merger.merge_view.bmp8 import read_bmp8
from valve_qc_merger.merge_view.discovery import load_model
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.project import Project
from valve_qc_merger.retarget.qc_build import parse_bodygroups, parse_sequences
from valve_qc_merger.services.base import CollectingReporter
from valve_qc_merger.services.decompile import DecompileOptions, run_decompile

_DIR = Path("tests/examples/mdl")
_SRC = _DIR / "src"


def _close(a, b, tol=1e-3) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def test_read_mdl_structure() -> None:
    model = read_mdl(_DIR / "mini.mdl")
    assert [b.name for b in model.bones] == ["root", "gun"]
    assert [p.name for p in model.bodyparts] == ["body", "gun"]
    assert [len(p.models) for p in model.bodyparts] == [1, 2]
    assert not model.bodyparts[1].models[0].meshes  # the blank
    textures = {t.name: t for t in model.textures}
    assert textures["glass.bmp"].flags & NF_MASKED
    idle, shoot = model.sequences
    assert (idle.label, idle.fps, idle.flags & 1, idle.activity) == ("idle", 15.0, 1, 1)
    assert [(e.event, e.frame, e.options) for e in idle.events] == [(5004, 1, "weapons/x.wav")]
    assert (shoot.numframes, shoot.events[0].event) == (3, 5001)
    assert model.attachments[0].bone == 1 and _close(model.attachments[0].org, (1, 2, 3))
    assert (model.hitboxes[0].group, model.hitboxes[0].bone) == (1, 1)
    assert model.controllers[0].type == MOTION_FLAGS["XR"]


def test_decompiled_geometry_and_winding_match_the_source() -> None:
    # Reference SMDs keep the stored bind (no root turn), so vertices come
    # back exactly where the source SMD had them, with the same winding.
    out = _decompile_tmp("mini")
    source = parse_smd_file(_SRC / "body.smd")
    result = parse_smd_file(out / "body.smd")

    # studiomdl stores vertices to ~0.01u; match each source triangle to a
    # result triangle in the SAME cyclic order (winding preserved).
    def cyclic(t):
        p = [tuple(v.position) for v in t.vertices]
        return [p, p[1:] + p[:1], p[2:] + p[:2]]

    assert len(result.triangles) == len(source.triangles)
    for want in source.triangles:
        target = [tuple(v.position) for v in want.vertices]
        assert any(
            all(_close(a, b, 0.02) for a, b in zip(rot, target, strict=True))
            for got in result.triangles for rot in cyclic(got)
        ), target


def test_decompiled_animation_undoes_the_root_turn() -> None:
    # studiomdl turns animation roots +90 deg about Z; the decompile turns
    # them back, so the SMD matches what was compiled.
    out = _decompile_tmp("mini")
    source = parse_smd_file(_SRC / "anims" / "shoot.smd")
    result = parse_smd_file(out / "anims" / "shoot.smd")
    assert len(result.frames) == len(source.frames)
    for fs, fr in zip(source.frames, result.frames, strict=True):
        for ps, pr in zip(fs.poses, fr.poses, strict=True):
            # animation positions are quantised to 1/256 units by studiomdl
            assert _close(ps.position, pr.position, 5e-3)
            delta = [(a - b + math.pi) % (2 * math.pi) - math.pi
                     for a, b in zip(ps.rotation, pr.rotation, strict=True)]
            assert max(abs(d) for d in delta) < 2e-3


def test_decompiled_qc_loads_and_keeps_metadata() -> None:
    out = _decompile_tmp("mini")
    qc = (out / "mini.qc").read_text(encoding="latin-1")
    assert parse_bodygroups(qc) == {"body": ["body"], "gun": ["gun"]}
    assert "\tblank" in qc.replace("    ", "\t")
    assert "$texrendermode glass.bmp masked" in qc
    assert '$attachment 0 "gun" 1 2 3' in qc
    assert "$cliptotextures" in qc
    idle = parse_sequences(qc)[0]
    assert (idle.fps, idle.loop) == (15.0, True)
    assert idle.events == ('{ event 5004 1 "weapons/x.wav" }',)
    assert "ACT_IDLE 1" in qc
    model = load_model(out)
    assert set(model.meshes) == {"body", "gun"} and set(model.anims) == {"idle", "shoot"}


def test_external_textures_are_read_from_the_t_file() -> None:
    out = _decompile_tmp("mini_ext")
    for name in ("skin.bmp", "glass.bmp"):
        got = read_bmp8((out / "maps_8bit" / name).read_bytes())
        want = read_bmp8((_SRC / name).read_bytes())
        assert (got.width, got.height, bytes(got.pixels)) == \
            (want.width, want.height, bytes(want.pixels))
        assert got.palette == want.palette


def test_decompile_service_skips_companions(tmp_path: Path) -> None:
    reporter = CollectingReporter()
    result = run_decompile(DecompileOptions(source=_DIR, out=tmp_path), reporter)
    assert result.ok, reporter.lines
    assert sorted(p.name for p in result.outputs) == ["mini.qc", "mini_ext.qc"]


def test_project_imports_mdl(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    added = project.import_mdl(_DIR / "mini.mdl")
    assert [a.name for a in added] == ["mini"]
    asset_dir = project.asset_dir("mini")
    assert (asset_dir / "mini.qc").exists() and (asset_dir / "anims" / "idle.smd").exists()
    assert project.assets["mini"].source.endswith("mini.mdl")
    assert not (tmp_path / "pack" / ".import").exists()


_CACHE: dict[str, Path] = {}


def _decompile_tmp(stem: str) -> Path:
    if stem not in _CACHE:
        import tempfile
        root = Path(tempfile.mkdtemp(prefix="mdltest_"))
        _CACHE[stem] = decompile_mdl(_DIR / f"{stem}.mdl", root).directory
    return _CACHE[stem]
