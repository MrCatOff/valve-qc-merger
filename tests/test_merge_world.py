"""merge-world tests: rendered-pose bake, merging, gate — over tests/examples/world.

Fixture models cover the corpus's structure classes:

- ``w_infinity``    — idle pose sits 9+ units from the bind pose (the bake
  actually moves the mesh);
- ``w_glockred``    — two-bone chain, QC without ``$sequence``, a bone name
  with a trailing space;
- ``w_bloodhunter`` — two root bones, both vertex-bearing;
- ``w_luger``       — ``$texturegroup`` skin rows + empty decompiler submodels.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from valve_qc_merger.cli import main
from valve_qc_merger.commands.merge_player import _load_player_model
from valve_qc_merger.merge_world.bake import bake_rendered_pose
from valve_qc_merger.parsers.smd import parse_smd_file

_EXAMPLES = Path("tests/examples/world")


def test_bake_moves_only_models_whose_idle_leaves_the_bind() -> None:
    moved = _load_player_model(_EXAMPLES / "w_infinity")
    assert bake_rendered_pose(moved).max_bake_delta > 1.0
    static = _load_player_model(_EXAMPLES / "w_luger")
    assert bake_rendered_pose(static).max_bake_delta < 0.01


def test_full_merge_single_part(tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    fixtures = ["w_bloodhunter", "w_glockred", "w_infinity", "w_luger"]
    for fixture in fixtures:
        shutil.copytree(_EXAMPLES / fixture, models_dir / fixture)
    out = tmp_path / "out"
    exit_code = main([
        "merge-world", str(models_dir), "--out", str(out), "--name", "w_test",
    ])
    assert exit_code == 0  # includes the verification gate

    qc = (out / "w_test.qc").read_text(encoding="latin-1")
    assert qc.count("studio ") == len(fixtures)
    assert qc.index("blank") < qc.index("studio ")
    assert '$sequence "idle"' in qc
    assert "$hbox" not in qc  # studiomdl auto-generates ONE box on 'weapon'
    assert '$texturegroup "skinfamilies"' in qc  # luger's 3 skin rows

    manifest = (out / "models.ini").read_text()
    for position, fixture in enumerate(fixtures, 1):
        assert f"[{fixture}]" in manifest
        assert f"pev_body = {position}" in manifest
    # Luger's skin rows are spelled out so a plugin can switch them by index.
    assert "skins = 3" in manifest
    assert "skin_0 = " in manifest and "skin_2 = " in manifest

    # Two bones total, identity transforms, every vertex on 'weapon'.
    for fixture in fixtures:
        mesh = parse_smd_file(out / "geometry" / f"{fixture}.smd")
        assert [(n.name, n.parent) for n in mesh.nodes] == \
            [("flash", -1), ("weapon", 0)]
        assert {v.bone for t in mesh.triangles for v in t.vertices} == {1}

    # The baked infinity mesh must NOT be bit-identical to its source (the
    # idle pose moved it), while luger's must be untouched.
    source = parse_smd_file(
        _EXAMPLES / "w_infinity" / "NEXON_CSO_w_Infinity_REF.smd"
    )
    merged = parse_smd_file(out / "geometry" / "w_infinity.smd")
    source_positions = {(v.position.x, v.position.y, v.position.z)
                       for t in source.triangles for v in t.vertices}
    merged_positions = {(v.position.x, v.position.y, v.position.z)
                       for t in merged.triangles for v in t.vertices}
    assert source_positions != merged_positions
