"""merge-player tests: bone collapse, merging, gate — over tests/examples/player.

Fixture models cover the corpus's structure classes:

- ``p_anaconda``  — flash -> weapon chain on the right hand (the common case);
- ``p_elite``     — dual-wield, one chain per hand;
- ``p_tknife``    — single bone already; QC has NO ``$sequence`` at all;
- ``p_luger``     — ``$texturegroup`` skin rows + empty decompiler submodels;
- ``p_balrog9``   — no weapon bones at all (verts ride shared Bip01 arm bones).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from valve_qc_merger.cli import main
from valve_qc_merger.commands.merge_player import _load_player_model
from valve_qc_merger.merge_player.analyze import collapse_weapon_bones
from valve_qc_merger.merge_player.merger import parse_texturegroups
from valve_qc_merger.merge_view.skeleton_ops import fk_worlds
from valve_qc_merger.parsers.smd import parse_smd_file

_EXAMPLES = Path("tests/examples/player")


def _world_of(smd_path: Path, bone: str) -> tuple[float, float, float]:
    smd = parse_smd_file(smd_path)
    index = next(n.index for n in smd.nodes if n.name == bone)
    world = fk_worlds(smd, smd.frames[0])[index].translation
    return (world.x, world.y, world.z)


def test_flash_chain_collapses_to_one_bone_in_place() -> None:
    model = _load_player_model(_EXAMPLES / "p_anaconda")
    pristine = _world_of(_EXAMPLES / "p_anaconda" / "P_reference_anaconda.smd",
                         "pw_ANA")
    plan = collapse_weapon_bones(model)
    assert [b.final for b in plan.bones] == ["p_anaconda"]
    assert plan.bones[0].anchor == "Bip01 R Hand"
    assert plan.bones[0].removed == ("flash",)
    mesh = max(model.meshes.values(), key=lambda m: len(m.nodes))
    names = [n.name for n in mesh.nodes]
    assert "flash" not in names and "p_anaconda" in names
    # The collapse is world-exact: the surviving bone did not move.
    index = next(n.index for n in mesh.nodes if n.name == "p_anaconda")
    world = fk_worlds(mesh, mesh.frames[0])[index].translation
    assert abs(world.x - pristine[0]) < 1e-4
    assert abs(world.y - pristine[1]) < 1e-4
    assert abs(world.z - pristine[2]) < 1e-4
    # Every vertex now binds to the surviving weapon bone.
    assert {v.bone for t in mesh.triangles for v in t.vertices} == {index}


def test_dual_wield_right_hand_owns_base_name() -> None:
    model = _load_player_model(_EXAMPLES / "p_elite")
    plan = collapse_weapon_bones(model)
    by_anchor = {b.anchor: b.final for b in plan.bones}
    assert by_anchor == {
        "Bip01 R Hand": "p_elite",
        "Bip01 L Hand": "p_elite_L",
    }


def test_shared_bone_only_model_needs_no_weapon_bones() -> None:
    model = _load_player_model(_EXAMPLES / "p_balrog9")
    plan = collapse_weapon_bones(model)
    assert plan.bones == []
    assert len(plan.shared) == 15  # both full arm chains


def test_qc_without_sequence_adopts_on_disk_idle() -> None:
    model = _load_player_model(_EXAMPLES / "p_tknife")
    assert model.anims  # picked up from p_tknife_anims/ despite the bare QC
    assert any("no $sequence" in w for w in model.warnings)


def test_full_merge_single_part(tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    fixtures = ["p_anaconda", "p_balrog9", "p_elite", "p_luger", "p_tknife"]
    for fixture in fixtures:
        shutil.copytree(_EXAMPLES / fixture, models_dir / fixture)
    out = tmp_path / "out"
    exit_code = main([
        "merge-player", str(models_dir), "--out", str(out), "--name", "p_test",
    ])
    assert exit_code == 0  # includes the verification gate on every part

    qc = (out / "p_test.qc").read_text(encoding="latin-1")
    # One weapons bodygroup: blank first, then one submodel per model.
    assert qc.count("studio ") == len(fixtures)
    assert qc.index("blank") < qc.index("studio ")
    # Luger's skin families survive as a merged texturegroup (3 rows).
    rows = parse_texturegroups(qc)
    assert len(rows) == 3 and all(len(r) == 1 for r in rows)
    assert '$sequence "idle"' in qc
    assert "$attachment" not in qc

    manifest = (out / "models.ini").read_text()
    for position, fixture in enumerate(fixtures, 1):
        assert f"[{fixture}]" in manifest
        assert f"pev_body = {position}" in manifest
    # Luger's skin rows are spelled out so a plugin can switch them by index.
    assert "skins = 3" in manifest
    assert "skin_0 = #256256Luger_P_08_Old_p.bmp" in manifest
    assert "skin_1 = #256256Luger_p_6.bmp" in manifest
    assert "skin_2 = #256256Luger_p_8.bmp" in manifest

    # All emitted SMDs share one table; weapon bones are model-named.
    idle = parse_smd_file(out / "animations" / "idle.smd")
    names = {n.name for n in idle.nodes}
    assert {"p_anaconda", "p_elite", "p_elite_L", "p_luger", "p_tknife"} <= names
    assert not any(n in names for n in ("flash", "lflash", "rflash"))
    for fixture in fixtures:
        mesh = parse_smd_file(out / "geometry" / f"{fixture}.smd")
        assert [(n.index, n.name, n.parent) for n in mesh.nodes] == \
            [(n.index, n.name, n.parent) for n in idle.nodes]


def test_empty_submodels_dropped_with_warning(tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    shutil.copytree(_EXAMPLES / "p_luger", models_dir / "p_luger")
    out = tmp_path / "out"
    assert main([
        "merge-player", str(models_dir), "--out", str(out), "--name", "p_one",
    ]) == 0
    qc = (out / "p_one.qc").read_text(encoding="latin-1")
    # Only the real reference mesh became a submodel (upgrade/upgrade_2 empty).
    assert qc.count("studio ") == 1
