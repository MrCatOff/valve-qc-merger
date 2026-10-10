"""Limits by compiler and server: submodels, pev_body, delta.lst."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from valve_qc_merger import limits
from valve_qc_merger.merge_view.bodygroups import ModelParts
from valve_qc_merger.merge_view.parts import view_body_range
from valve_qc_merger.server.delta import body_bits

_REGAMEDLL = """
entity_state_t gamedll Entity_Encode
{
	DEFINE_DELTA( skin, DT_SHORT | DT_SIGNED, 9, 1.0 ),
	DEFINE_DELTA( body, DT_INTEGER, 18, 1.0 ),
}
entity_state_player_t gamedll Player_Encode
{
	DEFINE_DELTA( body, DT_INTEGER, 9, 1.0 ),   // a comment
}
custom_entity_state_t gamedll Custom_Encode
{
	DEFINE_DELTA( body, DT_INTEGER, 8, 1.0 ),
}
"""


def test_delta_lst_body_bits(tmp_path: Path) -> None:
    path = tmp_path / "delta.lst"
    path.write_text(_REGAMEDLL)
    assert body_bits(path) == {"entity_state_t": 18, "entity_state_player_t": 9,
                               "custom_entity_state_t": 8}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake compiler")
def test_the_compiler_banner_sets_the_submodels(tmp_path: Path) -> None:
    ours = tmp_path / "ours"
    ours.write_text("#!/bin/sh\necho 'valve-qc-merger studiomdl: MAXSTUDIOMODELS 1024'\n")
    stock = tmp_path / "stock"
    stock.write_text("#!/bin/sh\necho 'usage: studiomdl ...'\n")
    for fake in (ours, stock):
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    assert limits.studiomdl_submodels(ours) == 1024
    assert limits.studiomdl_submodels(stock) == limits.STOCK_SUBMODELS
    assert limits.studiomdl_submodels(None) == limits.STOCK_SUBMODELS
    assert limits.submodels() == 32
    with limits.use(submodels=1024, body_bits=9):
        assert limits.submodels() == 1024 and limits.body_values() == 512
    assert limits.submodels() == 32 and limits.body_values() == 2 ** 32


def test_view_body_range() -> None:
    one = ModelParts(weapon_stems=[["w"]], hands_stem="h")
    shared = ModelParts(weapon_stems=[["w"]], hand_variants=["m", "f"])
    # every group leads with a blank: per-weapon hands (N + 1) x (N + 1)
    assert view_body_range([(None, one)] * 15) == 256
    assert view_body_range([(None, one)] * 16) == 289  # over a view model's byte
    # shared hands: (N + 1) x 3
    assert view_body_range([(None, shared)] * 84, shared_hands=True) == 255
