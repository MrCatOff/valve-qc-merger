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
