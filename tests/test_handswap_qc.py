"""handswap QC surgery: swapping the original hand bodygroup(s) for ours.

The stock 29 viewmodels each have a single-studio hands bodygroup, but the
project's own fixtures (and CSO ports) carry a male/female submodel switch —
one ``$bodygroup "hands"`` block with SEVERAL ``studio`` lines. Both must
collapse to a single ``studio "hands"`` with no dangling references.
"""

from __future__ import annotations

from valve_qc_merger.handswap import qc as qcmod


def _rewrite(text: str, drop: set[str]) -> str:
    # parse() needs a file; build a QcInfo directly for a pure-text rewrite.
    info = qcmod.QcInfo(path="mem.qc", text=text, modelname="v.mdl",
                        references=[], sequences=[], attachments=[],
                        hboxes=[], controllers=[])
    return qcmod.rewrite(info, drop_studios=drop, attachment_fixes={},
                         survivors=set())


def test_single_studio_hands_bodygroup_is_swapped() -> None:
    text = ('$bodygroup "weapon"\n{\n\tstudio "ref_gun"\n}\n'
            '$bodygroup "hands"\n{\n\tstudio "orig_hands"\n}\n')
    out = _rewrite(text, {"orig_hands"})
    assert out.count('studio "hands"') == 1
    assert "orig_hands" not in out
    assert 'studio "ref_gun"' in out          # weapon untouched


def test_multi_studio_hands_bodygroup_collapses_to_one() -> None:
    # male/female submodel switch: one bodygroup, two studio lines
    text = ('$bodygroup "weapon"\n{\n\tstudio "ref_gun"\n}\n'
            '$bodygroup "hands"\n{\n\tstudio "male_hand"\n'
            '\tstudio "female_hand"\n}\n')
    out = _rewrite(text, {"male_hand", "female_hand"})
    assert out.count('$bodygroup "hands"') == 1  # no duplicate appended
    assert out.count('studio "hands"') == 1
    assert "male_hand" not in out and "female_hand" not in out
    assert 'studio "ref_gun"' in out


def test_hands_bodygroup_appended_when_absent() -> None:
    # single-reference model (no dedicated hands bodygroup): ours is added
    text = '$bodygroup "weapon"\n{\n\tstudio "ref_gun"\n}\n'
    out = _rewrite(text, set())
    assert out.count('studio "hands"') == 1
    assert 'studio "ref_gun"' in out


def test_surviving_hand_named_group_renamed_to_weapon() -> None:
    # v_balrog9: a gauntlet whose weapon shell lives under $bodygroup "hands"
    # (studios "balrog9_hand_*"). The real bare hands ("v_ref_part*") are the
    # ones dropped; the surviving glove must NOT stay a second "hands" group
    # (would collide with the injected hands + is a duplicate $bodygroup).
    text = ('$bodygroup "weapon"\n{\n\tstudio "v_ref_part1"\n}\n'
            '$bodygroup "weapon"\n{\n\tstudio "v_ref_part2"\n}\n'
            '$bodygroup "hands"\n{\n\tstudio "balrog9_hand_male"\n'
            '\tstudio "balrog9_hand_female"\n}\n')
    out = _rewrite(text, {"v_ref_part1", "v_ref_part2"})
    assert out.count('$bodygroup "hands"') == 1     # only the injected one
    assert out.count('studio "hands"') == 1
    assert 'studio "balrog9_hand_male"' in out      # gauntlet kept
    assert '$bodygroup "weapon"' in out             # under a weapon group


def test_surviving_hand_named_group_with_partial_drop_renamed() -> None:
    # same collision, but one variant of the surviving group is also dropped
    text = ('$bodygroup "weapon"\n{\n\tstudio "gun"\n}\n'
            '$bodygroup "hands"\n{\n\tstudio "claw_keep"\n'
            '\tstudio "old_hand"\n}\n')
    out = _rewrite(text, {"gun", "old_hand"})
    assert out.count('$bodygroup "hands"') == 1     # injected only
    assert "old_hand" not in out                    # dropped variant gone
    assert 'studio "claw_keep"' in out              # survivor kept
    assert '$bodygroup "weapon"' in out
