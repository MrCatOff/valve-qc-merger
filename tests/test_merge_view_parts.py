"""merge-view part splitting tests (spec M5): studiomdl's 32-submodel cap."""

from __future__ import annotations

from pathlib import Path

from valve_qc_merger.merge_view.bodygroups import ModelParts
from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.merge_view.parts import PartBudget, split_parts
from valve_qc_merger.models.geometry import Vector2, Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd, Triangle, Vertex


def _mesh(material: str, offset: float = 0.0) -> Smd:
    zero = Vector3(0.0, 0.0, 0.0)
    vertex = Vertex(bone=0, position=Vector3(offset, 0.0, 0.0),
                    normal=Vector3(0.0, 0.0, 1.0), uv=Vector2(0.0, 0.0))
    return Smd(
        nodes=[Node(0, "Bip01", -1)],
        frames=[Frame(0, (BonePose(0, zero, zero),))],
        triangles=[Triangle(material, (vertex, vertex, vertex))],
    )


def _model(name: str, tmp_path: Path, material: str, hands: bool = True,
           offset: float = 0.0) -> tuple[ModelInput, ModelParts]:
    directory = tmp_path / name
    directory.mkdir(exist_ok=True)
    meshes = {"weapon": _mesh(material)}
    parts = ModelParts(weapon_stems=[["weapon"]])
    if hands:
        meshes["hand"] = _mesh(material, offset=offset)
        parts.hands_stem = "hand"
    model = ModelInput(
        name=name, directory=directory, qc_path=directory / f"{name}.qc",
        qc_text="", bodygroups={}, sequences=[], meshes=meshes,
    )
    return model, parts


def test_split_respects_submodel_budget_and_shares_identical_hands(
    tmp_path: Path,
) -> None:
    # 40 weapons with identical hands: submodels per part = weapons + 1.
    pairs = [_model(f"m{i:02d}", tmp_path, "tex.bmp") for i in range(40)]
    parts = split_parts(pairs, PartBudget(submodels=32, textures=80))
    assert len(parts) == 2
    assert len(parts[0]) == 31  # 31 weapons + 1 shared hands = 32 submodels
    assert len(parts[1]) == 9
    assert [m.name for m, _ in parts[0]][:2] == ["m00", "m01"]


def test_split_respects_texture_budget(tmp_path: Path) -> None:
    pairs = [
        _model(f"m{i}", tmp_path, f"tex{i}.bmp", hands=False) for i in range(6)
    ]
    parts = split_parts(pairs, PartBudget(submodels=32, textures=2))
    assert [len(p) for p in parts] == [2, 2, 2]


def test_split_distinct_hands_count_separately(tmp_path: Path) -> None:
    # Distinct hand meshes cannot be shared, so each occupies a submodel.
    pairs = [
        _model(f"m{i}", tmp_path, "tex.bmp", offset=float(i)) for i in range(4)
    ]
    parts = split_parts(pairs, PartBudget(submodels=4, textures=80))
    # Each model brings weapon + own hands = 2 submodels; 4 fits two models.
    assert [len(p) for p in parts] == [2, 2]
