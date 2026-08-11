"""retarget command: --category output routing into
storage/retarget/{category}/{model}. Pure Python — no Blender required."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from valve_qc_merger.commands import retarget as rt


def _args(**overrides: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    rt.RetargetCommand().configure(parser)
    argv = ["--weapon-dir", overrides.pop("weapon_dir", "a/b/v_foo")]
    for key, value in overrides.items():
        argv += [f"--{key.replace('_', '-')}", value]
    return parser.parse_args(argv)


# --- output routing -------------------------------------------------------- #

def test_out_dir_defaults_to_category_model() -> None:
    args = _args(category="pistols", weapon_dir="tmp/cso_nexon/pistols/v_anaconda")
    assert rt._resolve_out_dir(args) == Path("storage/retarget/pistols/v_anaconda")


def test_out_dir_default_category_is_uncategorized() -> None:
    args = _args(weapon_dir="whatever/v_x")
    assert rt._resolve_out_dir(args) == Path("storage/retarget/uncategorized/v_x")


def test_out_flag_overrides_category(tmp_path: Path) -> None:
    args = _args(weapon_dir="whatever/v_x", out=str(tmp_path))
    assert rt._resolve_out_dir(args) == tmp_path


@pytest.mark.parametrize("bad", ["../evil", "a/b", "", ".", ".."])
def test_bad_category_rejected(bad: str) -> None:
    args = _args(category=bad, weapon_dir="whatever/v_x")
    with pytest.raises(ValueError):
        rt._resolve_out_dir(args)
