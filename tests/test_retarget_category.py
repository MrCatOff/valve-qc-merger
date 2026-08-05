"""Stage-1 retarget command: --category output routing and the hand
compatibility split. Pure Python — no Blender required."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from valve_qc_merger.commands import retarget as rt
from valve_qc_merger.retarget.driver import DriverError

_REF = "storage/hands/reference_hands.smd"
_FOREIGN_HANDS = "tests/examples/pair_deagle/v_deagle/f_dea_Male_hand_Low.smd"


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
    with pytest.raises(DriverError):
        rt._resolve_out_dir(args)


# --- hand compatibility split ---------------------------------------------- #

def test_our_hands_are_compatible() -> None:
    # Only original_hands + reference are read by the split.
    inputs = SimpleNamespace(original_hands="storage/hands/male.smd", reference=_REF)
    compatible, detail = rt._hands_compatible(inputs)  # type: ignore[arg-type]
    assert compatible
    assert "native" in detail


def test_foreign_hands_are_incompatible() -> None:
    inputs = SimpleNamespace(original_hands=_FOREIGN_HANDS, reference=_REF)
    compatible, detail = rt._hands_compatible(inputs)  # type: ignore[arg-type]
    assert not compatible
    assert "foreign" in detail


def test_unmeasurable_hands_are_incompatible() -> None:
    inputs = SimpleNamespace(original_hands="does/not/exist.smd", reference=_REF)
    compatible, _ = rt._hands_compatible(inputs)  # type: ignore[arg-type]
    assert not compatible
