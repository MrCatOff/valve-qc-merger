"""merge-v M2 hand matching tests (spec §3.3)."""

from __future__ import annotations

from pathlib import Path

from valve_qc_merger.merge_view.discovery import load_model
from valve_qc_merger.merge_view.hands import (
    collision_guard,
    hand_bone_names,
    load_reference_rig,
    match_hands,
)
from valve_qc_merger.parsers.smd import parse_smd_file

_REFERENCE = Path("storage/hands/reference_hands.smd")


def test_reference_matches_itself_identically() -> None:
    reference = load_reference_rig(_REFERENCE)
    mesh = parse_smd_file(_REFERENCE)
    match = match_hands(mesh, reference)
    # Every mapped bone keeps its own name, Nubs included (identity match).
    assert match.renames
    assert all(old == new for old, new in match.renames.items())
    assert not match.held_reference
    assert not collision_guard(mesh, match.renames)


def test_grafted_rig_maps_onto_reference_names() -> None:
    # The anaconda's grafted rig: underscore Bip01 names, reversed forearms,
    # bullet/helper stubs, 3-joint fingers (no Nubs).
    reference = load_reference_rig(_REFERENCE)
    model = load_model(Path("tests/examples/v_anaconda"))
    mesh = max(model.meshes.values(), key=lambda m: len(m.nodes))
    include = hand_bone_names(model.meshes, model.bodygroups)
    assert include is not None and "Bip01_L_Hand" in include
    match = match_hands(mesh, reference, include)
    renames = match.renames
    assert renames["Bip01_L_Hand"] == "ValveBiped.Bip01_L_Hand"
    assert renames["Bip01_R_Hand"] == "ValveBiped.Bip01_R_Hand"
    assert renames["Bip01_L_Finger0"] == "ValveBiped.Bip01_L_Finger0"  # thumb to thumb
    assert renames["Bip01_R_Finger4"] == "ValveBiped.Bip01_R_Finger4"
    assert renames["Bip01_L_Forearm"] == "ValveBiped.Bip01_L_Forearm"  # reversed forearm mapped
    # 3-joint fingers: the reference Nubs stay unmatched.
    assert all(name.endswith("Nub") for name in match.held_reference)
    assert len(match.held_reference) == 10
    assert not collision_guard(mesh, renames)
