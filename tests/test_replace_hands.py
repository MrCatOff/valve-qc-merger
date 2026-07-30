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
def test_short_thumb_continues_along_the_weapon() -> None:
    # v_elite has a two-bone thumb; my three-bone thumb has a surplus joint.
    # Every one of my thumb segments must point along the weapon thumb direction
    # (continue straight), not fold back toward the tip or extend elsewhere.
    from valve_qc_merger.kinematics import world_transforms
    from valve_qc_merger.transform import Transform

    weapon = parse_smd_file(_elite() / "v_elite-PV.smd")
    hand = parse_smd_file(_hands() / "male.smd")
    links = build_hand_correspondences(weapon, hand)
    short = [
        (source, target)
        for link in links
        for source, target in link.finger_pairs
        if len(source.joints) == 2
    ]
    assert short, "expected a two-bone thumb in the elite rig"
    source, target = short[0]

    graft = HandGraft(weapon, hand, links)
    animation = parse_smd_file(_elite() / "v_elite_anims" / "draw.smd")
    out = graft.retarget_animation(animation)
    wi = {n.name: n.index for n in weapon.nodes}
    mi = {n.name: n.index for n in out.nodes}
    wn = {n.index: n.name for n in weapon.nodes}
    tn = {n.index: n.name for n in hand.nodes}

    def unit(world: dict[int, Transform], a: int, b: int) -> Vector3:
        d = Vector3(*(world[b].translation[k] - world[a].translation[k] for k in range(3)))
        length = d.length()
        return Vector3(d.x / length, d.y / length, d.z / length)

    for original, retargeted in zip(animation.frames, out.frames, strict=True):
        ww = world_transforms(weapon.nodes, original)
        mw = world_transforms(out.nodes, retargeted)
        weapon_dir = unit(ww, wi[wn[source.joints[0]]], wi[wn[source.joints[1]]])
        for k in range(len(target.joints) - 1):  # every one of my thumb segments
            my_dir = unit(mw, mi[tn[target.joints[k]]], mi[tn[target.joints[k + 1]]])
            cos = weapon_dir.x * my_dir.x + weapon_dir.y * my_dir.y + weapon_dir.z * my_dir.z
            assert cos > 1.0 - 1e-6


def test_clearance_no_penetration_returns_zero() -> None:
    # A gun mesh well away from the hand needs no slide.
    from valve_qc_merger.clearance import weapon_clearance_offset
    from valve_qc_merger.models.geometry import Vector2
    from valve_qc_merger.models.smd import Smd, Triangle, Vertex

    def tri(x: float) -> Triangle:
        uv = Vector2(0.0, 0.0)
        v = [
            Vertex(0, Vector3(x, 0, 0), Vector3(0, 0, 1), uv),
            Vertex(0, Vector3(x + 1, 0, 0), Vector3(0, 0, 1), uv),
            Vertex(0, Vector3(x, 1, 0), Vector3(0, 0, 1), uv),
        ]
        return Triangle("m", (v[0], v[1], v[2]))

    hand = Smd(triangles=[tri(0.0)])
    gun = Smd(triangles=[tri(100.0)])  # far away
    offset, before, after = weapon_clearance_offset(hand, gun, Vector3(1, 0, 0))
    assert before == 0 and after == 0
    assert offset == Vector3(0.0, 0.0, 0.0)


@requires_elite
def test_weapon_clearance_targets_the_original_overlap() -> None:
    from valve_qc_merger.clearance import gun_vertices_inside_hand, weapon_clearance_offset

    weapon = parse_smd_file(_elite() / "v_elite-PV.smd")
    hand = parse_smd_file(_hands() / "male.smd")
    original = parse_smd_file(_elite() / "f_elite_Male_hand_Low.smd")
    graft = HandGraft(weapon, hand, build_hand_correspondences(weapon, hand))
    gun = graft.weapon_reference_smd()

    baseline = gun_vertices_inside_hand(original, gun)
    ours = gun_vertices_inside_hand(graft.reference_smd(), gun)
    assert 0 < baseline < ours  # a normal grip overlaps; ours overlaps more

    offset, before, after = weapon_clearance_offset(
        graft.reference_smd(), gun, Vector3(0.0, -1.0, 0.0), target_inside=baseline
    )
    assert before == ours
    # Slid down to about the original hands' overlap -- not to zero.
    assert after <= baseline + 3
    assert offset.y < 0 and offset.x == 0 and offset.z == 0


@requires_elite
def test_finger_ik_lands_tips_on_the_weapon_fingertips() -> None:
    from valve_qc_merger.kinematics import world_transforms

    weapon = parse_smd_file(_elite() / "v_elite-PV.smd")
    hand = parse_smd_file(_hands() / "male.smd")
    links = build_hand_correspondences(weapon, hand)
    animation = parse_smd_file(_elite() / "v_elite_anims" / "idle.smd")
    weapon_world = world_transforms(weapon.nodes, animation.frames[0])
    tname = {n.index: n.name for n in hand.nodes}

    def tip_gaps(finger_ik: bool) -> list[float]:
        graft = HandGraft(weapon, hand, links, finger_ik=finger_ik)
        out = graft.retarget_animation(animation)
        mi = {n.name: n.index for n in out.nodes}
        out_world = world_transforms(out.nodes, out.frames[0])
        gaps = []
        for link in links:
            for source, target in link.finger_pairs:
                my = out_world[mi[tname[target.joints[-1]]]].translation
                wp = weapon_world[source.joints[-1]].translation
                gaps.append(((my.x - wp.x) ** 2 + (my.y - wp.y) ** 2 + (my.z - wp.z) ** 2) ** 0.5)
        return gaps

    aim = tip_gaps(finger_ik=False)
    ik = tip_gaps(finger_ik=True)
    assert max(aim) > 1.5  # aim alone overshoots (the long thumb)
    # Matching-length fingers curl onto the grip; no finger ends up further out.
    assert sorted(ik)[len(ik) // 2] < 0.3  # median finger tip lands on the grip
    assert all(i <= a + 1e-6 for i, a in zip(sorted(ik), sorted(aim), strict=True))
    assert sum(ik) < sum(aim)  # overall the fingers sit closer to the grip


@requires_elite
def test_index_curl_tucks_the_trigger_finger_without_moving_others() -> None:
    import math

    from valve_qc_merger.kinematics import world_transforms

    weapon = parse_smd_file(_elite() / "v_elite-PV.smd")
    hand = parse_smd_file(_hands() / "male.smd")
    links = build_hand_correspondences(weapon, hand)
    animation = parse_smd_file(_elite() / "v_elite_anims" / "draw.smd")
    frame = next(f for f in animation.frames if f.time == 32)
    weapon_world = world_transforms(weapon.nodes, frame)
    tname = {n.index: n.name for n in hand.nodes}

    def solve(curl: float) -> tuple[dict[str, Vector3], dict[str, float]]:
        graft = HandGraft(weapon, hand, links, finger_ik=True, index_curl=curl)
        out = graft.retarget_animation(animation)
        mi = {n.name: n.index for n in out.nodes}
        world = world_transforms(out.nodes, next(f for f in out.frames if f.time == 32))
        mids: dict[str, Vector3] = {}
        tip_gaps: dict[str, float] = {}
        for link in links:
            for source, target in link.finger_pairs:
                base = tname[target.joints[0]]
                mids[base] = world[mi[tname[target.joints[1]]]].translation
                my = world[mi[tname[target.joints[-1]]]].translation
                wp = weapon_world[source.joints[-1]].translation
                tip_gaps[base] = (
                    (my.x - wp.x) ** 2 + (my.y - wp.y) ** 2 + (my.z - wp.z) ** 2
                ) ** 0.5
        return mids, tip_gaps

    def dist(a: Vector3, b: Vector3) -> float:
        return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2) ** 0.5

    base_mids, base_gaps = solve(0.0)
    curl_mids, curl_gaps = solve(math.radians(20.0))

    for side in ("R", "L"):
        index = f"Bip01_{side}_Finger1"
        # The trigger finger folds deeper (its middle joint sinks into the guard).
        assert dist(curl_mids[index], base_mids[index]) > 0.15
        # ...while its tip stays on the trigger (the weapon fingertip contact).
        assert curl_gaps[index] < 0.15
        # Every other finger is left exactly at the game's authored grip.
        for other in (f"Bip01_{side}_Finger{i}" for i in (0, 2, 3, 4)):
            assert dist(curl_mids[other], base_mids[other]) < 1e-6


@requires_elite
def test_grip_slide_moves_wrapping_fingers_but_not_the_thumb() -> None:
    from valve_qc_merger.kinematics import world_transforms

    weapon = parse_smd_file(_elite() / "v_elite-PV.smd")
    hand = parse_smd_file(_hands() / "male.smd")
    links = build_hand_correspondences(weapon, hand)
    animation = parse_smd_file(_elite() / "v_elite_anims" / "draw.smd")
    tname = {n.index: n.name for n in hand.nodes}

    def tips(slide: Vector3) -> dict[str, Vector3]:
        graft = HandGraft(weapon, hand, links, finger_ik=True, grip_slide=slide)
        out = graft.retarget_animation(animation)
        mi = {n.name: n.index for n in out.nodes}
        world = world_transforms(out.nodes, next(f for f in out.frames if f.time == 32))
        return {
            tname[target.joints[0]]: world[mi[tname[target.joints[-1]]]].translation
            for link in links
            for _, target in link.finger_pairs
        }

    def dist(a: Vector3, b: Vector3) -> float:
        return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2) ** 0.5

    base = tips(Vector3(0.0, 0.0, 0.0))
    slid = tips(Vector3(0.0, 2.0, 0.0))
    for side in ("R", "L"):
        # The thumb stays put, so the sliding gun body clears it.
        assert dist(slid[f"Bip01_{side}_Finger0"], base[f"Bip01_{side}_Finger0"]) < 1e-6
        # The wrapping fingers follow the gun, so the grip is preserved.
        for wrap in (1, 2, 3, 4):
            name = f"Bip01_{side}_Finger{wrap}"
            assert dist(slid[name], base[name]) > 1.0


@requires_elite
def test_palm_seat_offset_matches_the_original_grip() -> None:
    from valve_qc_merger.clearance import palm_seat_offset

    weapon = parse_smd_file(_elite() / "v_elite-PV.smd")
    hand = parse_smd_file(_hands() / "male.smd")
    original = parse_smd_file(_elite() / "f_elite_Male_hand_Low.smd")
    links = build_hand_correspondences(weapon, hand)
    graft = HandGraft(weapon, hand, links)
    finger_bones = {
        joint for link in links for source, _ in link.finger_pairs for joint in source.joints
    }
    offset = palm_seat_offset(
        graft.reference_smd(),
        original,
        graft.weapon_reference_smd(),
        {"Bip01_R_Hand", "Bip01_L_Hand"},
        finger_bones,
    )
    # Elite's grip sits ~half a unit forward of the palm; seating pulls it back.
    assert 0.2 < offset.y < 0.9
    assert abs(offset.x) < 0.5


@requires_elite
def test_replace_hands_weight_transfer_keeps_the_weapon_skeleton(tmp_path: Path) -> None:
    # The default (weight-transfer) re-skins the hands onto the weapon's own bones:
    # no bones added/removed, animations kept, gun slid by the world offset.
    result = replace_hands(
        _elite(), _hands(), tmp_path / "out", weapon_offset=Vector3(0.0, -0.5, 0.1)
    )
    assert result.removed_bones == 0 and result.added_bones == 0
    assert result.weapon_bones == result.output_bones

    out = result.output_dir

    def skeleton(path: Path) -> tuple[tuple[int, str, int], ...]:
        return tuple((n.index, n.name, n.parent) for n in parse_smd_file(path).nodes)

    reference = skeleton(_elite() / "v_elite-PV.smd")  # the weapon's own skeleton
    assert skeleton(out / "grafted_male.smd") == reference
    assert skeleton(out / "v_elite-PV.smd") == reference
    for animation in sorted((out / "v_elite_anims").glob("*.smd")):
        assert skeleton(animation) == reference  # animations untouched, share the rig

    # The gun mesh was slid by the world offset (not the hands).
    from valve_qc_merger.kinematics import world_transforms

    orig = parse_smd_file(_elite() / "v_elite-PV.smd")
    built = parse_smd_file(out / "v_elite-PV.smd")
    w = world_transforms(orig.nodes, orig.frames[0])
    ov, bv = orig.triangles[0].vertices[0], built.triangles[0].vertices[0]
    o = w[ov.bone].transform_point(ov.position)
    b = w[bv.bone].transform_point(bv.position)
    assert abs(b.y - o.y + 0.5) < 1e-3 and abs(b.z - o.z - 0.1) < 1e-3


@requires_elite
def test_replace_hands_on_elite_is_consistent(tmp_path: Path) -> None:
    result = replace_hands(_elite(), _hands(), tmp_path / "out", use_graft=True)
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
    result = replace_hands(_anaconda(), _hands(), tmp_path / "out", use_graft=True)
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


@requires_samples
def test_weight_transfer_hand_articulates_across_finger_bones(tmp_path: Path) -> None:
    # The hand must be re-skinned onto the weapon's own hand/finger bones (as the
    # original hands were) so the weapon's animations articulate the fingers --
    # not bound to a single rigid bone. So it should spread over many bones.
    out = replace_hands(_anaconda(), _hands(), tmp_path / "out").output_dir
    hand = parse_smd_file(out / "grafted_male.smd")
    used = {v.bone for t in hand.triangles for v in t.vertices}
    assert len(used) >= 12


def _model_bounds(path: Path) -> tuple[Vector3, Vector3]:
    positions = [v.position for t in parse_smd_file(path).triangles for v in t.vertices]
    lo = Vector3(*(min(p[a] for p in positions) for a in range(3)))
    hi = Vector3(*(max(p[a] for p in positions) for a in range(3)))
    return lo, hi


@requires_samples
def test_weight_transfer_hand_is_stored_in_model_space_on_the_gun(tmp_path: Path) -> None:
    # Reference SMD vertices are model space (like the gun and graft path); a
    # bone-local hand would sit at the bone origin, off the gun. So the hand's raw
    # positions must overlap the gun's on every axis.
    out = replace_hands(_anaconda(), _hands(), tmp_path / "out").output_dir
    hlo, hhi = _model_bounds(out / "grafted_male.smd")
    glo, ghi = _model_bounds(out / "ref_Anaconda.smd")
    for axis in range(3):
        assert hlo[axis] <= ghi[axis] and glo[axis] <= hhi[axis]
