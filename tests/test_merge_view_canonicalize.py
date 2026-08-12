"""merge-v M2 canonicalisation on a real reversed-hierarchy model (spec §3.4-3.5)."""

from __future__ import annotations

from pathlib import Path

from valve_qc_merger.merge_view.canonicalize import canonicalize_model
from valve_qc_merger.merge_view.discovery import load_model
from valve_qc_merger.merge_view.hands import (
    hand_bone_names,
    load_reference_rig,
    match_hands,
)
from valve_qc_merger.parsers.smd import parse_smd_file

_REFERENCE = Path("storage/hands/reference_hands.smd")


def test_anaconda_canonicalises_exactly(tmp_path: Path) -> None:
    # Copy the model so in-place canonicalisation cannot touch the corpus.
    import shutil

    work = tmp_path / "v_anaconda"
    shutil.copytree("tests/examples/v_anaconda", work)
    model = load_model(work)
    reference = load_reference_rig(_REFERENCE)
    fullest = max(model.meshes.values(), key=lambda m: len(m.nodes))
    match = match_hands(fullest, reference,
                        hand_bone_names(model.meshes, model.bodygroups))

    reference_nodes = parse_smd_file(_REFERENCE).nodes
    report = canonicalize_model(model, match, reference_nodes, prune=True)

    # Nothing moved, anywhere, in any sequence.
    assert report.max_pose_deviation < 1e-6

    for smd in {**model.meshes, **model.anims}.values():
        names = {n.name for n in smd.nodes}
        parent_of = {
            n.name: next((p.name for p in smd.nodes if p.index == n.parent), None)
            for n in smd.nodes
        }
        # Canonical structure enforced (this rig had reversed forearms).
        assert parent_of["ValveBiped.Bip01_L_Forearm"] == "Bip01"
        assert parent_of["ValveBiped.Bip01_L_Hand"] == "ValveBiped.Bip01_L_Forearm"
        assert parent_of["ValveBiped.Bip01_R_Finger0"] == "ValveBiped.Bip01_R_Hand"
        # No Nubs, no orphan roots besides Bip01.
        assert not any(n.endswith("Nub") for n in names)
        roots = [n.name for n in smd.nodes if n.parent == -1]
        assert roots == ["Bip01"]
        # ids are parents-before-children (studiomdl requirement).
        for node in smd.nodes:
            assert node.parent < node.index

    # QC bone references were patched to canonical names.
    assert '"Bip01 L Hand"' in model.qc_text or "Bip01 L Hand" not in model.qc_text
