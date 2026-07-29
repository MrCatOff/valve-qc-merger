"""Tests for the text-level QC bodygroup editor."""

from __future__ import annotations

from valve_qc_merger.qc_document import (
    find_bodygroup,
    find_bodygroups,
    replace_bodygroup_studios,
)

_QC = """$modelname "v_test.mdl"
$bodygroup "weapon"
{
	studio "ref_weapon"
}
$bodygroup "hands"
{
	studio "Hand"
	studio "f_female_hand_Low"
}
$sequence "idle" { "anims\\idle" fps 16 }
"""


def test_find_bodygroups() -> None:
    blocks = find_bodygroups(_QC)
    assert [b.name for b in blocks] == ["weapon", "hands"]
    assert blocks[0].studios == ("ref_weapon",)
    assert blocks[1].studios == ("Hand", "f_female_hand_Low")


def test_replace_hands_bodygroup_preserves_rest() -> None:
    hands = find_bodygroup(_QC, "hands")
    assert hands is not None
    updated = replace_bodygroup_studios(_QC, hands, ["grafted_male", "grafted_female"])
    # The hands bodygroup now points at the new studios ...
    reparsed = find_bodygroup(updated, "hands")
    assert reparsed is not None
    assert reparsed.studios == ("grafted_male", "grafted_female")
    # ... and everything else is untouched.
    assert '$modelname "v_test.mdl"' in updated
    assert '$sequence "idle"' in updated
    assert find_bodygroup(updated, "weapon") is not None
    assert "Hand" not in find_bodygroup(updated, "hands").studios  # type: ignore[union-attr]
