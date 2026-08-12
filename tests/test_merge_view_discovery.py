"""merge-v M1 tests: discovery, sanitisation, loading (spec §3.1–3.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from valve_qc_merger.merge_view.discovery import (
    MergeViewError,
    discover_models,
    load_model,
    sanitize_model_dir,
)


def _model(tmp_path: Path, name: str, *, qcs: int = 1) -> Path:
    d = tmp_path / name
    (d / "anims").mkdir(parents=True)
    for i in range(qcs):
        (d / f"{name}{i or ''}.qc").write_text(
            '$modelname "m.mdl"\n'
            '$bodygroup "weapon"\n{\n\tstudio "gun"\n}\n'
            '$sequence "idle" {\n\t"anims\\idle"\n\tfps 30\n}\n'
        )
    smd = ("version 1\nnodes\n  0 \"Bip01\" -1\nend\nskeleton\n  time 0\n"
           "    0 0 0 0 0 0 0\nend\n")
    (d / "gun.smd").write_text(smd + "triangles\nend\n")
    (d / "anims" / "idle.smd").write_text(smd)
    return d


def test_discovery_finds_models_and_rejects_ambiguity(tmp_path: Path) -> None:
    _model(tmp_path, "v_a")
    _model(tmp_path, "v_b")
    (tmp_path / "textures_only").mkdir()  # no QC: skipped silently
    found = discover_models(tmp_path, exclude={"v_b"})
    assert [d.name for d in found] == ["v_a"]

    _model(tmp_path, "v_dup", qcs=2)
    with pytest.raises(MergeViewError, match="2 .qc files"):
        discover_models(tmp_path)


def test_sanitise_renames_non_ascii_and_patches_references(tmp_path: Path) -> None:
    d = _model(tmp_path, "v_kr")
    weird = "손텍스처.bmp"  # Korean: "hand texture"
    (d / weird).write_bytes(b"BM" + b"\x00" * 52)
    qc = d / "v_kr.qc"
    qc.write_text(qc.read_text() + f'\n// texture {weird}\n')

    renames = sanitize_model_dir(d)
    assert weird in renames
    new = renames[weird]
    assert new.isascii() and (d / new).exists() and not (d / weird).exists()
    assert new in qc.read_text() and weird not in qc.read_text()


def test_load_model_reads_manifest_and_fails_loudly(tmp_path: Path) -> None:
    d = _model(tmp_path, "v_ok")
    model = load_model(d)
    assert model.name == "v_ok"
    assert set(model.meshes) == {"gun"}
    assert set(model.anims) == {"idle"}
    assert model.bone_names == ["Bip01"]

    (d / "anims" / "idle.smd").unlink()
    with pytest.raises(MergeViewError, match="no animation SMDs"):
        load_model(d)
