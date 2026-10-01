"""merge-v shared attachment slots: GoldSrc keeps 4 attachments per MODEL, so
every weapon's sequences drive shared slot bones to its own muzzle points."""

from __future__ import annotations

from pathlib import Path

from valve_qc_merger.merge_view.bodygroups import ModelParts
from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.merge_view.merger import merge_models
from valve_qc_merger.merge_view.skeleton_ops import fk_worlds
from valve_qc_merger.models.geometry import Vector2, Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd, Triangle, Vertex
from valve_qc_merger.parsers.smd import parse_smd_file

ZERO = Vector3(0.0, 0.0, 0.0)


def _smd(gun: str, gun_pos: list[Vector3], *, mesh: bool) -> Smd:
    v = Vertex(bone=1, position=Vector3(1.0, 2.0, 3.0),
               normal=Vector3(0.0, 0.0, 1.0), uv=Vector2(0.0, 0.0))
    return Smd(
        nodes=[Node(0, "Bip01", -1), Node(1, gun, 0)],
        frames=[Frame(i, (BonePose(0, ZERO, ZERO), BonePose(1, p, ZERO)))
                for i, p in enumerate(gun_pos)],
        triangles=[Triangle("tex.bmp", (v, v, v))] if mesh else [],
    )


def _model(name: str, gun: str, qc: str, anim_pos: list[Vector3],
           tmp_path: Path) -> tuple[ModelInput, ModelParts]:
    directory = tmp_path / name
    directory.mkdir()
    (directory / "tex.bmp").write_bytes(b"BM" + b"\0" * 10)
    model = ModelInput(
        name=name, directory=directory, qc_path=directory / f"{name}.qc",
        qc_text=qc, bodygroups={}, sequences=[],
        meshes={"weapon": _smd(gun, [ZERO], mesh=True)},
        anims={"shoot": _smd(gun, anim_pos, mesh=False)},
    )
    return model, ModelParts(weapon_stems=[["weapon"]])


def _slot_world(smd: Smd, slot: int, frame: int) -> Vector3:
    index = next(n.index for n in smd.nodes if n.name == f"attachment{slot}")
    return fk_worlds(smd, smd.frames[frame])[index].translation


def test_each_weapon_drives_the_shared_slots_to_its_own_muzzle(tmp_path: Path) -> None:
    a = _model("v_a", "gun_a", '$attachment 0 "gun_a" 0 -5 1\n'
               '$attachment 1 "gun_a" 0 -2 0\n',
               [Vector3(10.0, 0.0, 0.0), Vector3(11.0, 0.0, 0.0)], tmp_path)
    b = _model("v_b", "gun_b", "$attachment 0 gun_b 3 0 0\n",
               [Vector3(0.0, 20.0, 0.0), Vector3(0.0, 21.0, 0.0)], tmp_path)
    out = tmp_path / "out"
    report = merge_models([a, b], out, "v_m")

    qc = (out / "v_m.qc").read_text(encoding="latin-1")
    assert report.attachments == 2
    assert '$attachment 0 "attachment0" 0 0 0' in qc
    assert '$attachment 1 "attachment1" 0 0 0' in qc
    assert "gun_a" not in qc.split("$attachment", 1)[1].split("$sequence")[0]

    anim_a = parse_smd_file(out / "v_a" / "shoot.smd")
    anim_b = parse_smd_file(out / "v_b" / "v_b__shoot.smd")
    assert _slot_world(anim_a, 0, 1) == Vector3(11.0, -5.0, 1.0)
    assert _slot_world(anim_a, 1, 0) == Vector3(10.0, -2.0, 0.0)
    assert _slot_world(anim_b, 0, 1) == Vector3(3.0, 21.0, 0.0)
    assert _slot_world(anim_b, 1, 0) == ZERO  # v_b has no slot 1: parked

    # Slot bones carry a zero-area anchor so studiomdl keeps them.
    weapon = parse_smd_file(out / "v_a" / "weapon.smd")
    slot_bones = {n.index for n in weapon.nodes if n.name.startswith("attachment")}
    anchored = {v.bone for t in weapon.triangles for v in t.vertices}
    assert slot_bones <= anchored


def test_off_map_slot_gets_its_own_carrier_so_near_poses_stay_precise(
        tmp_path: Path) -> None:
    # v_far hides its shell eject a million units away (CSO idiom); studiomdl
    # quantises a bone channel with ONE scale over all sequences, so that
    # value must not share a channel with v_near's real muzzle.
    near = _model("v_near", "gun_n", '$attachment 0 "gun_n" 0 0 0\n'
                  '$attachment 1 "gun_n" 0 -2 0\n',
                  [Vector3(10.0, 0.0, 0.0)], tmp_path)
    far = _model("v_far", "gun_f", '$attachment 0 "gun_f" 0 0 0\n'
                 '$attachment 1 "gun_f" -1000000 0 -50\n',
                 [Vector3(0.0, 5.0, 0.0)], tmp_path)
    out = tmp_path / "out"
    report = merge_models([near, far], out, "v_m")

    assert report.attachments == 2
    anim_n = parse_smd_file(out / "v_near" / "shoot.smd")
    anim_f = parse_smd_file(out / "v_far" / "v_far__shoot.smd")
    names = [n.name for n in anim_n.nodes]
    assert "attachment1_base" in names and "attachment0_base" not in names
    leaf = next(n for n in anim_n.nodes if n.name == "attachment1")
    assert anim_n.nodes[leaf.parent].name == "attachment1_base"

    def local(smd: Smd, name: str) -> Vector3:
        index = next(n.index for n in smd.nodes if n.name == name)
        return smd.frames[0].pose_for(index).position

    # near weapon: carrier holds the muzzle, leaf channel stays exactly 0
    assert local(anim_n, "attachment1") == ZERO
    assert _slot_world(anim_n, 1, 0) == Vector3(10.0, -2.0, 0.0)
    # far weapon: carrier at rest, leaf holds the off-map point
    assert local(anim_f, "attachment1_base") == ZERO
    assert _slot_world(anim_f, 1, 0) == Vector3(-1000000.0, 5.0, -50.0)


def _two_hand_smd(gun_pos: list[Vector3], *, mesh: bool) -> Smd:
    # Bip01 -> Hand.L (x=-10), Hand.R (x=+10); a gun bone in each hand.
    v = Vertex(bone=3, position=Vector3(1.0, 2.0, 3.0),
               normal=Vector3(0.0, 0.0, 1.0), uv=Vector2(0.0, 0.0))
    left, right = Vector3(-10.0, 0.0, 0.0), Vector3(10.0, 0.0, 0.0)
    return Smd(
        nodes=[Node(0, "Bip01", -1), Node(1, "Hand.L", 0),
               Node(2, "Hand.R", 0), Node(3, "gun_l", 1), Node(4, "gun_r", 2)],
        frames=[Frame(i, (BonePose(0, ZERO, ZERO), BonePose(1, left, ZERO),
                          BonePose(2, right, ZERO), BonePose(3, p, ZERO),
                          BonePose(4, p, ZERO)))
                for i, p in enumerate(gun_pos)],
        triangles=[Triangle("tex.bmp", (v, v, v))] if mesh else [],
    )


def test_slots_hang_off_the_wrist_they_stay_closest_to(tmp_path: Path) -> None:
    # Elite-style: slot 0 = left muzzle, slot 1 = right muzzle.
    directory = tmp_path / "v_dual"
    directory.mkdir()
    (directory / "tex.bmp").write_bytes(b"BM" + b"\0" * 10)
    model = ModelInput(
        name="v_dual", directory=directory, qc_path=directory / "v_dual.qc",
        qc_text='$attachment 0 "gun_l" 0 -5 0\n$attachment 1 "gun_r" 0 -5 0\n',
        bodygroups={}, sequences=[],
        meshes={"weapon": _two_hand_smd([ZERO], mesh=True)},
        anims={"shoot": _two_hand_smd([ZERO, Vector3(0.0, 1.0, 0.0)],
                                      mesh=False)},
    )
    out = tmp_path / "out"
    merge_models([(model, ModelParts(weapon_stems=[["weapon"]]))], out, "v_m")

    anim = parse_smd_file(out / "v_dual" / "shoot.smd")
    name_of = {n.index: n.name for n in anim.nodes}
    parent = {n.name: name_of.get(n.parent) for n in anim.nodes}
    assert parent["attachment0"] == "Hand.L"
    assert parent["attachment1"] == "Hand.R"
    assert _slot_world(anim, 0, 1) == Vector3(-10.0, -4.0, 0.0)
    assert _slot_world(anim, 1, 1) == Vector3(10.0, -4.0, 0.0)
