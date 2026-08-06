"""Tests for the non-Blender surface of the retarget pipeline.

The Blender worker (Phases 1-5) is exercised by its own integration harness;
here we cover input resolution, the §5 node-table gate, config handling and CLI
wiring, all of which run in ordinary Python.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from valve_qc_merger.cli import build_parser
from valve_qc_merger.retarget.config import RetargetConfig
from valve_qc_merger.retarget.driver import (
    DriverError,
    assert_identical_node_tables,
    find_blender,
    resolve_inputs,
    run_sequence,
)


def _write_smd(path: Path, nodes: list[tuple[int, str, int]], *, mesh: bool) -> None:
    lines = ["version 1", "nodes"]
    lines += [f'{i} "{name}" {parent}' for i, name, parent in nodes]
    lines += ["end", "skeleton", "time 0"]
    lines += [f"{i} 0 0 0 0 0 0" for i, _, _ in nodes]
    lines.append("end")
    if mesh:
        lines += ["triangles", "end"]
    path.write_text("\n".join(lines) + "\n")


_RIG = [(0, "Bone01", -1), (1, "Bone02", 0), (2, "Bone03", 1)]
_BIP = [(0, "Bip01", -1), (1, "Bip01 R Hand", 0)]


def _weapon_dir(tmp_path: Path, *, anim_rig: list[tuple[int, str, int]] | None = None) -> Path:
    d = tmp_path / "v_elite"
    (d / "v_elite_anims").mkdir(parents=True)
    _write_smd(d / "v_elite-PV.smd", _RIG, mesh=True)
    _write_smd(d / "f_elite_Male_hand_Low.smd", _RIG, mesh=True)
    _write_smd(d / "v_elite_anims" / "idle.smd", anim_rig or _RIG, mesh=False)
    _write_smd(d / "v_elite_anims" / "draw.smd", anim_rig or _RIG, mesh=False)
    return d


def test_resolve_inputs_discovers_weapon_hands_and_anims(tmp_path: Path) -> None:
    ref = tmp_path / "reference_hands.smd"
    _write_smd(ref, _BIP, mesh=True)
    inputs = resolve_inputs(ref, _weapon_dir(tmp_path), "v_elite_anims/*.smd")
    assert inputs.weapon_pv.name == "v_elite-PV.smd"
    assert inputs.original_hands.name == "f_elite_Male_hand_Low.smd"
    assert set(inputs.sequences) == {"idle", "draw"}


def test_resolve_inputs_only_subset(tmp_path: Path) -> None:
    ref = tmp_path / "reference_hands.smd"
    _write_smd(ref, _BIP, mesh=True)
    inputs = resolve_inputs(ref, _weapon_dir(tmp_path), "v_elite_anims/*.smd", only={"idle"})
    assert set(inputs.sequences) == {"idle"}


def test_resolve_inputs_unknown_sequence_raises(tmp_path: Path) -> None:
    ref = tmp_path / "reference_hands.smd"
    _write_smd(ref, _BIP, mesh=True)
    with pytest.raises(DriverError, match="not found"):
        resolve_inputs(ref, _weapon_dir(tmp_path), "v_elite_anims/*.smd", only={"reload"})


def test_node_table_gate_passes_on_shared_rig(tmp_path: Path) -> None:
    ref = tmp_path / "reference_hands.smd"
    _write_smd(ref, _BIP, mesh=True)
    inputs = resolve_inputs(ref, _weapon_dir(tmp_path), "v_elite_anims/*.smd")
    table = assert_identical_node_tables(inputs)
    assert table == [(0, "Bone01", -1), (1, "Bone02", 0), (2, "Bone03", 1)]


def test_node_table_gate_fails_on_divergent_anim(tmp_path: Path) -> None:
    ref = tmp_path / "reference_hands.smd"
    _write_smd(ref, _BIP, mesh=True)
    mismatched = [(0, "Bone01", -1), (1, "BoneX", 0), (2, "Bone03", 1)]
    inputs = resolve_inputs(ref, _weapon_dir(tmp_path, anim_rig=mismatched), "v_elite_anims/*.smd")
    with pytest.raises(DriverError, match="node table"):
        assert_identical_node_tables(inputs)


def test_find_blender_prefers_explicit_config(tmp_path: Path) -> None:
    fake = tmp_path / "blender"
    fake.write_text("")
    assert find_blender(RetargetConfig(blender=str(fake))) == str(fake)


def test_find_blender_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VQM_BLENDER", raising=False)
    monkeypatch.setattr("valve_qc_merger.retarget.driver.shutil.which", lambda _: None)
    monkeypatch.setattr("valve_qc_merger.retarget.driver._MAC_BLENDER", "/nope/blender")
    with pytest.raises(DriverError, match="Blender not found"):
        find_blender(RetargetConfig(blender="/does/not/exist"))


def test_config_roundtrip_and_unknown_key() -> None:
    cfg = RetargetConfig()
    restored = RetargetConfig.from_dict(cfg.to_job_dict())
    assert restored == cfg
    assert restored.anchor_policy == "wrist"
    assert restored.weapon_offset is None
    with pytest.raises(ValueError, match="unknown config keys"):
        RetargetConfig.from_dict({"nonsense": 1})


def _fake_blender(tmp_path: Path, body: str) -> str:
    """A stand-in 'blender' executable (a python script) for driver tests."""
    script = tmp_path / "fake_blender"
    script.write_text("#!/usr/bin/env python3\nimport sys, json\n" + body)
    script.chmod(0o755)
    return str(script)


def _inputs(tmp_path: Path):  # type: ignore[no-untyped-def]
    ref = tmp_path / "reference_hands.smd"
    _write_smd(ref, _BIP, mesh=True)
    return resolve_inputs(ref, _weapon_dir(tmp_path), "v_elite_anims/*.smd", only={"idle"})


def test_run_sequence_fails_when_worker_writes_no_report(tmp_path: Path) -> None:
    # Blender exits 0 but the worker crashed before writing a report: must NOT PASS.
    blender = _fake_blender(tmp_path, "sys.exit(0)\n")
    result = run_sequence(blender, _inputs(tmp_path), "idle", RetargetConfig(), tmp_path / "out")
    assert not result.ok
    assert result.report["status"] == "FAIL"
    assert result.report["kind"] == "worker-crash"


def test_run_sequence_threads_dry_run_into_the_job(tmp_path: Path) -> None:
    body = (
        "job = json.load(open(sys.argv[sys.argv.index('--job') + 1]))\n"
        "json.dump({'status': 'MAPPED' if job['dry_run'] else 'RETARGETED',\n"
        "           'dry_run': job['dry_run']}, open(job['report'], 'w'))\n"
    )
    blender = _fake_blender(tmp_path, body)
    inputs = _inputs(tmp_path)
    dry = run_sequence(blender, inputs, "idle", RetargetConfig(), tmp_path / "out", dry_run=True)
    assert dry.ok and dry.report["dry_run"] is True and dry.report["status"] == "MAPPED"
    wet = run_sequence(blender, inputs, "idle", RetargetConfig(), tmp_path / "out", dry_run=False)
    assert wet.report["dry_run"] is False and wet.report["status"] == "RETARGETED"


def test_cli_registers_retarget_command() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["retarget", "--reference", "r.smd", "--weapon-dir", "w", "--out", "o"]
    )
    assert args.command == "retarget"
    assert args.dry_run is False


def test_variant_skeleton_mismatch_is_rejected(tmp_path: Path) -> None:
    from valve_qc_merger.retarget.driver import assert_variant_skeletons

    ref = tmp_path / "reference_hands.smd"
    _write_smd(ref, _BIP, mesh=True)
    good = tmp_path / "male.smd"
    _write_smd(good, _BIP, mesh=True)
    bad = tmp_path / "female.smd"
    _write_smd(bad, [(0, "Bip01", -1), (1, "Bip01 L Hand", 0)], mesh=True)  # renamed bone

    inputs = resolve_inputs(
        ref, _weapon_dir(tmp_path), "v_elite_anims/*.smd",
        hand_variants={"male": str(good)},
    )
    assert_variant_skeletons(inputs)  # matching variant passes

    inputs = resolve_inputs(
        ref, _weapon_dir(tmp_path / "w2"), "v_elite_anims/*.smd",
        hand_variants={"female": str(bad)},
    )
    with pytest.raises(DriverError, match="different node table"):
        assert_variant_skeletons(inputs)


def test_inputs_resolve_from_the_qc_manifest(tmp_path: Path) -> None:
    # No --anims, no --weapon-pv, no --original-hands: everything comes from the
    # QC's $bodygroup and $sequence blocks (backslash paths, no extensions).
    d = tmp_path / "weapon"
    (d / "anims").mkdir(parents=True)
    _write_smd(d / "gun_mesh.smd", _RIG, mesh=True)
    _write_smd(d / "hands_mesh.smd", _RIG, mesh=True)
    _write_smd(d / "anims" / "idle.smd", _RIG, mesh=False)
    _write_smd(d / "anims" / "fire.smd", _RIG, mesh=False)
    (d / "v_gun.qc").write_text(
        '$modelname "v_gun.mdl"\n'
        '$bodygroup "weapon"\n{\n\tstudio "gun_mesh"\n}\n'
        '$bodygroup "hands"\n{\n\tstudio "hands_mesh"\n}\n'
        '$sequence "idle" {\n\t"anims\\idle"\n\tfps 16\n}\n'
        '$sequence "fire" {\n\t"anims\\fire"\n\t{ event 5001 0 "11" }\n\tfps 30\n}\n'
    )
    ref = tmp_path / "reference_hands.smd"
    _write_smd(ref, _BIP, mesh=True)

    inputs = resolve_inputs(ref, d)
    assert inputs.weapon_pv.name == "gun_mesh.smd"
    assert inputs.original_hands.name == "hands_mesh.smd"
    assert set(inputs.sequences) == {"idle", "fire"}
    assert inputs.sequences["fire"].name == "fire.smd"


def test_inputs_collect_every_weapon_studio_part(tmp_path: Path) -> None:
    # A weapon split across several always-on $bodygroup "weapon" blocks
    # (bloodhunter: pistol body + blood projectile + effects) must surface ALL
    # parts, not just the first — parse_bodygroups de-dups them to weapon,
    # weapon_2, weapon_3. weapon_pv stays the first part (the rig source).
    d = tmp_path / "weapon"
    (d / "anims").mkdir(parents=True)
    for name in ("gun_left", "gun_right01", "gun_right02", "hands_mesh"):
        _write_smd(d / f"{name}.smd", _RIG, mesh=True)
    _write_smd(d / "anims" / "idle.smd", _RIG, mesh=False)
    (d / "v_gun.qc").write_text(
        '$modelname "v_gun.mdl"\n'
        '$bodygroup "weapon"\n{\n\tstudio "gun_left"\n}\n'
        '$bodygroup "weapon"\n{\n\tstudio "gun_right01"\n}\n'
        '$bodygroup "weapon"\n{\n\tstudio "gun_right02"\n}\n'
        '$bodygroup "hands"\n{\n\tstudio "hands_mesh"\n}\n'
        '$sequence "idle" {\n\t"anims\\idle"\n\tfps 16\n}\n'
    )
    ref = tmp_path / "reference_hands.smd"
    _write_smd(ref, _BIP, mesh=True)

    inputs = resolve_inputs(ref, d)
    assert inputs.weapon_pv.name == "gun_left.smd"
    assert [p.name for p in inputs.weapon_studios] == [
        "gun_left.smd", "gun_right01.smd", "gun_right02.smd"
    ]
