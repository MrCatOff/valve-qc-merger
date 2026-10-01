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
