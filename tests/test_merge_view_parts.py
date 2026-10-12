"""merge-v part splitting tests (spec M5): studiomdl's 32-submodel cap."""

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


def test_split_respects_submodel_budget_with_per_model_hands(
    tmp_path: Path,
) -> None:
    # 40 weapons, each bringing its own hands: 2 submodels per model, plus the
    # blank each of the two groups leads with and the weapon group's body-2
    # blank: 2N + 3 <= 32 -> 14
    pairs = [_model(f"m{i:02d}", tmp_path, "tex.bmp") for i in range(40)]
    parts = split_parts(pairs, PartBudget(submodels=32, textures=80))
    assert [len(p) for p in parts] == [14, 14, 12]
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
    parts = split_parts(pairs, PartBudget(submodels=7, textures=80))
    # Each model brings weapon + own hands = 2 submodels, plus the two groups'
    # leading blanks and the body-2 blank: 7 fits two models.
    assert [len(p) for p in parts] == [2, 2]


def test_split_shared_hands_counts_one_hands_group(tmp_path: Path) -> None:
    # --shared-hands: ONE hands group (male/female) for the whole part, so a
    # part takes 27 weapons + their blank + {blank, male, blank, female} = 32.
    pairs = []
    for i in range(40):
        model, parts = _model(f"m{i:02d}", tmp_path, "tex.bmp")
        parts.hand_variants = ["hands_female", "hands_male"]
        pairs.append((model, parts))
    parts = split_parts(pairs, PartBudget(submodels=32, textures=80),
                        shared_hands=True)
    assert [len(p) for p in parts] == [27, 13]  # N + blank + 4 hands entries <= 32
