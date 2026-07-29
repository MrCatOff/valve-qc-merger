"""End-to-end tests for the retargeting pipeline and replace-hands command.

These exercise the real sample assets under ``tmp/`` and are skipped when the
samples are not present (e.g. a checkout without the working data).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from valve_qc_merger.commands.replace_hands import parse_offset, replace_hands
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
def test_graft_replaces_hand_bones_without_doubling() -> None:
    weapon = parse_smd_file(_anaconda() / "ref_Anaconda.smd")
    hand = parse_smd_file(_hands() / "male.smd")
    graft = HandGraft(weapon, hand, build_hand_correspondences(weapon, hand))

    merged = graft.merged_nodes()
    merged_names = {n.name for n in merged}
    # The weapon's wrist and finger bones are gone, not kept alongside mine.
    assert "Bone_Lefthand" not in merged_names
    assert not any(name.startswith("Bone05") for name in merged_names)
    assert graft.removed_count() == 32  # 2 wrists + 30 finger bones
    assert graft.added_count() == 34
    assert len(merged) == 53  # 51 - 32 + 34, no doubling
    # Gun/structural bones survive.
    assert "Bone03" in merged_names and "Bone_Rullet" in merged_names

    animation = parse_smd_file(_anaconda() / "v_anaconda_anims" / "draw.smd")
    out = graft.retarget_animation(animation)
    assert out.frame_count == animation.frame_count
    for frame in out.frames:
        assert {p.bone for p in frame.poses} == {n.index for n in merged}


@requires_samples
def test_gun_bones_world_motion_is_preserved() -> None:
    from valve_qc_merger.kinematics import world_transforms

    weapon = parse_smd_file(_anaconda() / "ref_Anaconda.smd")
    hand = parse_smd_file(_hands() / "male.smd")
    graft = HandGraft(weapon, hand, build_hand_correspondences(weapon, hand))
    animation = parse_smd_file(_anaconda() / "v_anaconda_anims" / "shoot1.smd")
    out = graft.retarget_animation(animation)

    old_index = {n.name: n.index for n in weapon.nodes}
    new_index = {n.name: n.index for n in out.nodes}
    shared = {n.name for n in weapon.nodes} & {n.name for n in out.nodes}
    for original, retargeted in zip(animation.frames, out.frames, strict=True):
        before = world_transforms(weapon.nodes, original)
        after = world_transforms(out.nodes, retargeted)
        for name in shared:
            a = before[old_index[name]].translation
            b = after[new_index[name]].translation
            assert (a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2 < 1e-12


def test_parse_offset() -> None:
    identity = parse_offset("0,0,0,0,0,0")
    assert identity.translation == (0.0, 0.0, 0.0)
    moved = parse_offset("0,0,0,1.5,-2,3")
    assert moved.translation == (1.5, -2.0, 3.0)
    with pytest.raises(ValueError):
        parse_offset("1,2,3")


@requires_samples
def test_offset_moves_hands_but_not_the_gun() -> None:
    from valve_qc_merger.kinematics import world_transforms

    weapon = parse_smd_file(_anaconda() / "ref_Anaconda.smd")
    hand = parse_smd_file(_hands() / "male.smd")
    links = build_hand_correspondences(weapon, hand)
    offset = {"L": parse_offset("0,0,0,5,0,0"), "R": parse_offset("0,0,0,5,0,0")}
    graft = HandGraft(weapon, hand, links, offset)
    animation = parse_smd_file(_anaconda() / "v_anaconda_anims" / "draw.smd")
    out = graft.retarget_animation(animation)

    new_index = {n.name: n.index for n in out.nodes}
    old_index = {n.name: n.index for n in weapon.nodes}
    shared = {n.name for n in weapon.nodes} & {n.name for n in out.nodes}
    before = world_transforms(weapon.nodes, animation.frames[0])
    after = world_transforms(out.nodes, out.frames[0])
    # Gun/structural bones are unaffected by the offset ...
    for name in shared:
        a = before[old_index[name]].translation
        b = after[new_index[name]].translation
        assert (a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2 < 1e-12
    # ... but the reference hand actually moved.
    hand_pos = after[new_index["Bip01_L_Hand"]].translation
    no_offset = HandGraft(weapon, hand, links).retarget_animation(animation)
    base_index = {n.name: n.index for n in no_offset.nodes}
    hand_pos_no_offset = world_transforms(no_offset.nodes, no_offset.frames[0])[
        base_index["Bip01_L_Hand"]
    ].translation
    assert hand_pos != hand_pos_no_offset


@requires_samples
def test_replace_hands_writes_compilable_build(tmp_path: Path) -> None:
    result = replace_hands(_anaconda(), _hands(), tmp_path / "out")
    assert result.variants == ("male", "female")
    assert result.output_bones == 53
    assert result.removed_bones == 32
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
