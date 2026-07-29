"""End-to-end tests for the retargeting pipeline and replace-hands command.

These exercise the real sample assets under ``tmp/`` and are skipped when the
samples are not present (e.g. a checkout without the working data).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from valve_qc_merger.commands.replace_hands import (
    parse_offset,
    parse_translation,
    replace_hands,
)
from valve_qc_merger.correspondence import build_hand_correspondences
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget import HandGraft


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _anaconda() -> Path:
    return _repo_root() / "tmp" / "pistols" / "view" / "v_anaconda"


def _hands() -> Path:
    return _repo_root() / "tmp" / "hands"


def _elite() -> Path:
    return _repo_root() / "tmp" / "pistols" / "view" / "v_elite"


requires_samples = pytest.mark.skipif(
    not (_anaconda().exists() and _hands().exists()),
    reason="sample assets under tmp/ are not available",
)

requires_elite = pytest.mark.skipif(
    not (_elite().exists() and _hands().exists()),
    reason="v_elite sample not available",
)


@requires_elite
def test_structural_wrist_detection_and_short_thumb() -> None:
    # v_elite names no Bone_Lefthand/Bone_Righthand (wrists are found structurally),
    # its fingers hang off a palm, and one thumb is only two bones.
    weapon = parse_smd_file(_elite() / "v_elite-PV.smd")
    hand = parse_smd_file(_hands() / "male.smd")
    links = build_hand_correspondences(weapon, hand)
    assert {link.side for link in links} == {"L", "R"}
    lengths = {len(source.joints) for link in links for source, _ in link.finger_pairs}
    assert 2 in lengths and 3 in lengths  # short thumb + normal fingers
    for link in links:
        assert len(link.finger_pairs) == 5


@requires_elite
def test_short_thumb_curls_to_the_grip() -> None:
    # v_elite has a two-bone thumb; my three-bone thumb has a surplus joint.
    # It must curl its tip onto the weapon thumb tip, not extend straight through.
    from valve_qc_merger.kinematics import world_transforms

    weapon = parse_smd_file(_elite() / "v_elite-PV.smd")
    hand = parse_smd_file(_hands() / "male.smd")
    links = build_hand_correspondences(weapon, hand)
    short = [
        (link, source, target)
        for link in links
        for source, target in link.finger_pairs
        if len(source.joints) == 2
    ]
    assert short, "expected a two-bone thumb in the elite rig"
    _, source, target = short[0]

    graft = HandGraft(weapon, hand, links)
    animation = parse_smd_file(_elite() / "v_elite_anims" / "draw.smd")
    out = graft.retarget_animation(animation)
    weapon_tip = {n.name: n.index for n in weapon.nodes}
    out_tip = {n.name: n.index for n in out.nodes}
    weapon_names = {n.index: n.name for n in weapon.nodes}
    hand_names = {n.index: n.name for n in hand.nodes}

    for original, retargeted in zip(animation.frames, out.frames, strict=True):
        weapon_world = world_transforms(weapon.nodes, original)
        out_world = world_transforms(out.nodes, retargeted)
        my_tip = out_world[out_tip[hand_names[target.tip]]].translation
        grip = weapon_world[weapon_tip[weapon_names[source.tip]]].translation
        distance = ((my_tip.x - grip.x) ** 2 + (my_tip.y - grip.y) ** 2 + (my_tip.z - grip.z) ** 2)
        assert distance < 4.0  # within 2 units of the grip point


@requires_elite
def test_replace_hands_on_elite_is_consistent(tmp_path: Path) -> None:
    result = replace_hands(_elite(), _hands(), tmp_path / "out")
    out = result.output_dir

    def skeleton(path: Path) -> tuple[tuple[int, str, int], ...]:
        return tuple((n.index, n.name, n.parent) for n in parse_smd_file(path).nodes)

    reference = skeleton(out / "grafted_male.smd")
    assert skeleton(out / "v_elite-PV.smd") == reference
    for animation in sorted((out / "v_elite_anims").glob("*.smd")):
        assert skeleton(animation) == reference
    names = {name for _, name, _ in reference}
    assert "Bip01_L_Hand" in names and "Bip01_R_Hand" in names


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
def test_fingers_track_the_weapon_directions() -> None:
    from valve_qc_merger.kinematics import world_transforms
    from valve_qc_merger.models.geometry import Vector3
    from valve_qc_merger.transform import Transform

    weapon = parse_smd_file(_anaconda() / "ref_Anaconda.smd")
    hand = parse_smd_file(_hands() / "male.smd")
    links = build_hand_correspondences(weapon, hand)
    graft = HandGraft(weapon, hand, links)
    animation = parse_smd_file(_anaconda() / "v_anaconda_anims" / "draw.smd")
    out = graft.retarget_animation(animation)

    wi = {n.name: n.index for n in weapon.nodes}
    mi = {n.name: n.index for n in out.nodes}
    wname = {n.index: n.name for n in weapon.nodes}
    tname = {n.index: n.name for n in hand.nodes}

    def direction(world: dict[int, Transform], a: int, b: int) -> Vector3:
        pa = world[a].translation
        pb = world[b].translation
        d = Vector3(pb.x - pa.x, pb.y - pa.y, pb.z - pa.z)
        length = d.length()
        return Vector3(d.x / length, d.y / length, d.z / length)

    for frame in (animation.frames[3], animation.frames[15]):
        out_frame = out.frames[animation.frames.index(frame)]
        weapon_world = world_transforms(weapon.nodes, frame)
        out_world = world_transforms(out.nodes, out_frame)
        for link in links:
            for source, target in link.finger_pairs:
                for k in range(2):  # root->mid and mid->tip segments
                    ws, wt = source.joints[k], source.joints[k + 1]
                    ts, tt = target.joints[k], target.joints[k + 1]
                    wd = direction(weapon_world, wi[wname[ws]], wi[wname[wt]])
                    md = direction(out_world, mi[tname[ts]], mi[tname[tt]])
                    cos = wd.x * md.x + wd.y * md.y + wd.z * md.z
                    assert cos > 1.0 - 1e-6  # my finger bone points where the weapon's does


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


def test_parse_translation() -> None:
    assert parse_translation("1,-2,3.5") == Vector3(1.0, -2.0, 3.5)
    with pytest.raises(ValueError):
        parse_translation("1,2,3,4")


@requires_samples
def test_weapon_offset_shifts_gun_not_hands() -> None:
    weapon = parse_smd_file(_anaconda() / "ref_Anaconda.smd")
    hand = parse_smd_file(_hands() / "male.smd")
    links = build_hand_correspondences(weapon, hand)
    plain = HandGraft(weapon, hand, links)
    pushed = HandGraft(weapon, hand, links, None, Vector3(0.0, 0.0, 2.0))

    # Gun geometry moves by the offset ...
    for before, after in zip(
        plain.weapon_reference_smd().triangles,
        pushed.weapon_reference_smd().triangles,
        strict=True,
    ):
        for v0, v1 in zip(before.vertices, after.vertices, strict=True):
            assert abs((v1.position.z - v0.position.z) - 2.0) < 1e-9
    # ... while the hand mesh is untouched.
    for before, after in zip(
        plain.reference_smd().triangles, pushed.reference_smd().triangles, strict=True
    ):
        assert before.vertices[0].position == after.vertices[0].position


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

    # Every SMD in the build shares ONE identical skeleton (index, name, parent):
    # the hand reference, the gun reference and every animation.
    def skeleton(path: Path) -> tuple[tuple[int, str, int], ...]:
        return tuple((n.index, n.name, n.parent) for n in parse_smd_file(path).nodes)

    reference_skel = skeleton(out / "grafted_male.smd")
    assert skeleton(out / "grafted_female.smd") == reference_skel
    assert skeleton(out / "ref_Anaconda.smd") == reference_skel  # gun re-expressed
    for animation in sorted((out / "v_anaconda_anims").glob("*.smd")):
        assert skeleton(animation) == reference_skel

    # No stale old-hand bones anywhere, and the old hand-mesh SMDs are removed.
    names = {name for _, name, _ in reference_skel}
    assert "Bone_Lefthand" not in names and "Bip01_L_Hand" in names
    assert not (out / "Hand.smd").exists()

    # Valid structure: parents resolve, geometry references existing bones.
    reference = parse_smd_file(out / "grafted_male.smd")
    indices = {n.index for n in reference.nodes}
    for node in reference.nodes:
        assert node.parent == -1 or node.parent in indices
    for triangle in reference.triangles:
        assert all(vertex.bone in indices for vertex in triangle.vertices)
