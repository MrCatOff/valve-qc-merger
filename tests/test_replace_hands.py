"""End-to-end tests for the retargeting pipeline and replace-hands command.

These exercise the real sample assets under ``tmp/`` and are skipped when the
samples are not present (e.g. a checkout without the working data).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from valve_qc_merger.commands.replace_hands import replace_hands
from valve_qc_merger.correspondence import build_hand_correspondences
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget import HandGraft


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _anaconda() -> Path:
    return _repo_root() / "tmp" / "pistols" / "view" / "v_anaconda"


def _hands() -> Path:
    return _repo_root() / "tmp" / "hands"


requires_samples = pytest.mark.skipif(
    not (_anaconda().exists() and _hands().exists()),
    reason="sample assets under tmp/ are not available",
)


@requires_samples
def test_correspondence_matches_all_fingers() -> None:
    weapon = parse_smd_file(_anaconda() / "ref_Anaconda.smd")
    hand = parse_smd_file(_hands() / "male.smd")
    links = build_hand_correspondences(weapon, hand)
    assert {link.side for link in links} == {"L", "R"}
    names = {n.index: n.name for n in hand.nodes}
    for link in links:
        assert len(link.finger_pairs) == 5
        # Every reference finger is matched to a distinct weapon finger chain.
        source_roots = [src.root for src, _ in link.finger_pairs]
        assert len(set(source_roots)) == 5
        target_names = [names[tgt.root] for _, tgt in link.finger_pairs]
        assert target_names == [f"Bip01_{link.side}_Finger{n}" for n in range(5)]


@requires_samples
def test_graft_preserves_weapon_and_adds_hand_bones() -> None:
    weapon = parse_smd_file(_anaconda() / "ref_Anaconda.smd")
    hand = parse_smd_file(_hands() / "male.smd")
    graft = HandGraft(weapon, hand, build_hand_correspondences(weapon, hand))

    merged = graft.merged_nodes()
    weapon_names = {n.name for n in weapon.nodes}
    merged_names = {n.name for n in merged}
    assert weapon_names <= merged_names  # weapon skeleton untouched
    assert len(merged) == len(weapon.nodes) + 34  # 17 kept bones per hand

    animation = parse_smd_file(_anaconda() / "v_anaconda_anims" / "draw.smd")
    out = graft.retarget_animation(animation)
    assert out.frame_count == animation.frame_count
    for frame in out.frames:
        assert {p.bone for p in frame.poses} == {n.index for n in merged}


@requires_samples
def test_replace_hands_writes_compilable_build(tmp_path: Path) -> None:
    result = replace_hands(_anaconda(), _hands(), tmp_path / "out")
    assert result.variants == ("male", "female")
    assert result.grafted_bones == 34
    assert result.animations_retargeted == 6

    out = result.output_dir
    assert (out / "grafted_male.smd").exists()
    assert (out / "grafted_female.smd").exists()
    assert (out / "male.bmp").exists()

    # QC now points at the grafted hands.
    qc_text = (out / "v_anaconda.qc").read_text(encoding="latin-1")
    assert "grafted_male" in qc_text and "grafted_female" in qc_text

    # Reference and animations share one skeleton, by name, with valid structure.
    reference = parse_smd_file(out / "grafted_male.smd")
    reference_names = {n.name for n in reference.nodes}
    indices = {n.index for n in reference.nodes}
    for node in reference.nodes:
        assert node.parent == -1 or node.parent in indices
    for triangle in reference.triangles:
        assert all(vertex.bone in indices for vertex in triangle.vertices)
    for animation in sorted((out / "v_anaconda_anims").glob("*.smd")):
        assert {n.name for n in parse_smd_file(animation).nodes} == reference_names
