"""merge-view --shared-hands: one shared male/female hands bodygroup so pev_body
stays hand+weapon*2 (< 255) instead of weapon*hands."""

from __future__ import annotations

from pathlib import Path

from valve_qc_merger.merge_view.bodygroups import ModelParts, collapse_bodygroups
from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.merge_view.merger import merge_models
from valve_qc_merger.models.geometry import Vector2, Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd, Triangle, Vertex


def _mesh(material: str, weapon: bool = False) -> Smd:
    # Shared 2-bone skeleton (Bip01 root + a gun bone under it), so the models
    # unify onto one root. Hands weight to Bip01, the weapon to the gun bone.
    zero = Vector3(0.0, 0.0, 0.0)
    bone = 1 if weapon else 0
    v = Vertex(bone=bone, position=Vector3(0.0, 0.0, 0.0),
               normal=Vector3(0.0, 0.0, 1.0), uv=Vector2(0.0, 0.0))
    return Smd(nodes=[Node(0, "Bip01", -1), Node(1, "gun", 0)],
               frames=[Frame(0, (BonePose(0, zero, zero), BonePose(1, zero, zero)))],
               triangles=[Triangle(material, (v, v, v))])


def _model(name: str, tmp_path: Path) -> tuple[ModelInput, ModelParts]:
    directory = tmp_path / name
    directory.mkdir(exist_ok=True)
    (directory / "tex.bmp").write_bytes(b"BM" + b"\0" * 10)
    meshes = {"weapon": _mesh("tex.bmp", weapon=True),
              "hands_female": _mesh("tex.bmp"), "hands_male": _mesh("tex.bmp")}
    parts = ModelParts(weapon_stems=[["weapon"]], hands_stem="hands_female",
                       hand_variants=["hands_female", "hands_male"])
    model = ModelInput(name=name, directory=directory,
                       qc_path=directory / f"{name}.qc", qc_text="",
                       bodygroups={}, sequences=[], meshes=meshes)
    return model, parts


def test_shared_hands_emits_one_shared_hands_bodygroup(tmp_path: Path) -> None:
    pairs = [_model(f"v_w{i}", tmp_path) for i in range(3)]
    out = tmp_path / "out"
    report = merge_models(pairs, out, "v_merged", shared_hands=True)

    qc = (out / "v_merged.qc").read_text(encoding="latin-1")
    # exactly two bodyparts: weapon (N) + hands (2 shared), NOT per-weapon hands
    assert report.bodyparts == 2
    assert '$bodygroup "weapon"' in qc and '$bodygroup "hands"' in qc
    assert qc.index('$bodygroup "hands"') < qc.index('$bodygroup "weapon"')  # hands first
    assert qc.count('studio "hands/hands_female"') == 1
    assert qc.count('studio "hands/hands_male"') == 1
    for i in range(3):
        assert f'studio "v_w{i}/weapon"' in qc
    # shared hands written ONCE, not per-model
    assert (out / "hands" / "hands_female.smd").exists()
    assert (out / "hands" / "hands_male.smd").exists()
    assert not (out / "v_w0" / "hands.smd").exists()


def test_shared_hands_pev_body_is_weapon_times_two(tmp_path: Path) -> None:
    # hands first (stride 1, 2 entries), weapon second (stride 2): pev_body base
    # == position * 2, the male hand adds the low-order +1 at runtime, so worst
    # case 2N-1 stays well under 255.
    pairs = [_model(f"v_w{i}", tmp_path) for i in range(4)]
    report = merge_models(pairs, tmp_path / "out", "v_merged", shared_hands=True)
    assert [report.pev_body[f"v_w{i}"] for i in range(4)] == [0, 2, 4, 6]
    assert max(report.pev_body.values()) < 255


def test_collapse_keeps_hand_variants(tmp_path: Path) -> None:
    directory = tmp_path / "v_w"
    directory.mkdir()
    model = ModelInput(
        name="v_w", directory=directory, qc_path=directory / "v_w.qc",
        qc_text="", sequences=[],
        bodygroups={"weapon": ["weapon"], "hands": ["hands_female", "hands_male"]},
        meshes={"weapon": _mesh("w.bmp", weapon=True),
                "hands_female": _mesh("h.bmp"), "hands_male": _mesh("h.bmp")},
    )
    parts = collapse_bodygroups(model)
    assert parts.hand_variants == ["hands_female", "hands_male"]
    assert parts.hands_stem == "hands_female"


def _big_mesh(material: str, verts: int) -> Smd:
    """A weapon mesh with ``verts`` distinct positions (weighted to the gun bone)."""
    tris = [
        Triangle(material, tuple(
            Vertex(bone=1, position=Vector3(float(i), 0.0, 0.0),
                   normal=Vector3(0.0, 0.0, 1.0), uv=Vector2(0.0, 0.0))
            for _ in range(3)))
        for i in range(verts)
    ]
    zero = Vector3(0.0, 0.0, 0.0)
    return Smd(nodes=[Node(0, "Bip01", -1), Node(1, "gun", 0)],
               frames=[Frame(0, (BonePose(0, zero, zero), BonePose(1, zero, zero)))],
               triangles=tris)


def test_multipart_weapon_collapses_to_multiple_submodels(tmp_path: Path) -> None:
    # Two always-on weapon pieces whose combined vertices exceed the 2048 budget
    # -> two weapon submodels. --shared-hands rejects any such weapon (each extra
    # weapon bodygroup multiplies pev_body past 255 once merged).
    directory = tmp_path / "v_w"
    directory.mkdir()
    model = ModelInput(
        name="v_w", directory=directory, qc_path=directory / "v_w.qc",
        qc_text="", sequences=[],
        bodygroups={"body": ["a"], "barrel": ["b"], "hands": ["hands_female"]},
        meshes={"a": _big_mesh("w.bmp", 1500), "b": _big_mesh("w.bmp", 1500),
                "hands_female": _mesh("h.bmp")},
    )
    parts = collapse_bodygroups(model)
    assert len(parts.weapon_stems) > 1
