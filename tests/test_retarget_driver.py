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


def test_cli_registers_retarget_command() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["retarget", "--reference", "r.smd", "--weapon-dir", "w", "--out", "o"]
    )
    assert args.command == "retarget"
    assert args.dry_run is False
