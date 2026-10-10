"""merge-props: any models (effects, props, claws) into a few models."""

from __future__ import annotations

import configparser
import re
import shutil
from pathlib import Path

from valve_qc_merger.services.base import CollectingReporter
from valve_qc_merger.services.decompile import DecompileOptions, run_decompile
from valve_qc_merger.services.merge_props import (
    MergePropsOptions,
    body_range,
    prop_parts,
    run_merge_props,
)
from valve_qc_merger.services.qc_check import check_qc

_MINI = Path(__file__).parent / "examples" / "mdl" / "mini.mdl"


def _models(tmp_path: Path, names: list[str]) -> Path:
    staged = tmp_path / "decompiled"
    run_decompile(DecompileOptions(source=_MINI, out=staged), CollectingReporter())
    inputs = tmp_path / "in"
    for name in names:
        shutil.copytree(staged / "mini", inputs / name)
        qc = next((inputs / name).glob("*.qc"))
        qc.rename(qc.with_name(f"{name}.qc"))
    return inputs


def test_props_merge_into_one_model_body_per_model(tmp_path: Path) -> None:
    inputs = _models(tmp_path, ["fx_a", "fx_b", "muzzle"])
    # a model with no mesh at all (bones + an attachment, like muzzle_*.mdl)
    qc = inputs / "muzzle" / "muzzle.qc"
    text = qc.read_text(encoding="latin-1")
    text = re.sub(r"(?m)^\$body\s.*$", "", text).replace('studio "gun"', "blank")
    assert "$attachment 0" in text
    qc.write_text(text, encoding="latin-1")

    out = tmp_path / "out"
    reporter = CollectingReporter()
    result = run_merge_props(MergePropsOptions(models_dir=inputs, out=out, name="fx"),
                             reporter)
    assert result.ok, reporter.lines
    assert result.outputs == [out / "fx.qc"]
    manifest = configparser.ConfigParser()
    manifest.read(out / "models.ini")
    assert [manifest[m]["pev_body"] for m in ("fx_a", "fx_b", "muzzle")] == ["0", "1", "2"]
    assert all(k.startswith("anim_") for k in manifest["fx_b"] if k != "pev_body")
    merged = (out / "fx.qc").read_text(encoding="latin-1")
    assert merged.count("$bodygroup") == 1 and "$attachment 0" in merged
    assert not [p for p in check_qc(out / "fx.qc") if p.level == "error"]
    muzzle = next(m for m in result.data["inventory"] if m["name"] == "muzzle")
    assert any("anchor" in w for w in muzzle["warnings"])


def test_body_range_counts_extra_groups(tmp_path: Path) -> None:
    from valve_qc_merger.merge_view.bodygroups import ModelParts
    one = ModelParts(weapon_stems=[["a"]])
    two = ModelParts(weapon_stems=[["a"], ["b"]])
    assert body_range([(None, one), (None, one)]) == 2  # type: ignore[list-item]
    assert body_range([(None, one), (None, two), (None, two)]) == 9  # type: ignore[list-item]
    inputs = _models(tmp_path, ["fx"])
    from valve_qc_merger.merge_view.discovery import load_model
    parts = prop_parts(load_model(inputs / "fx", require_anims=False))
    assert parts.weapon_stems and parts.hands_stem is None
