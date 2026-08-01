"""Greedy part splitting for merge-player.

The binding budget is studiomdl's hard 32-submodel array (one leading
``blank`` + one submodel per weapon = 31 weapons per part); textures and the
127-bone table are checked too but rarely bind (one weapon costs 1-2 bones).
"""

from __future__ import annotations

from dataclasses import dataclass

from valve_qc_merger.merge_player.analyze import PlayerPlan
from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.merge_view.merger import (
    BONE_LIMIT,
    SUBMODEL_LIMIT,
    _sanitize_material,
)

TEXTURE_BUDGET = 80  # soft default; the hard engine cap is 100


@dataclass
class PlayerBudget:
    """Per-part budgets (submodel count includes the leading blank)."""

    submodels: int = SUBMODEL_LIMIT
    textures: int = TEXTURE_BUDGET
    bones: int = BONE_LIMIT


def _model_textures(model: ModelInput, extra: set[str]) -> set[str]:
    materials = {
        _sanitize_material(t.material)
        for smd in model.meshes.values()
        for t in smd.triangles
    }
    return materials | extra


def split_player_parts(
    pairs: list[tuple[ModelInput, PlayerPlan]],
    budget: PlayerBudget,
    *,
    skin_textures: dict[str, set[str]] | None = None,
) -> list[list[tuple[ModelInput, PlayerPlan]]]:
    """Greedy in-order packing under the part budgets.

    ``skin_textures`` maps a model name to extra texture files its
    ``$texturegroup`` skin rows pull in beyond the mesh-referenced set.
    """
    parts: list[list[tuple[ModelInput, PlayerPlan]]] = []
    current: list[tuple[ModelInput, PlayerPlan]] = []
    bones: set[str] = set()
    textures: set[str] = set()
    for model, plan in pairs:
        model_bones = set(plan.shared) | {b.final for b in plan.bones}
        model_tex = _model_textures(
            model, (skin_textures or {}).get(model.name, set())
        )
        fits = (
            len(current) + 1 + 1 <= budget.submodels  # weapons + leading blank
            and len(bones | model_bones) <= budget.bones
            and len(textures | model_tex) <= budget.textures
        )
        if current and not fits:
            parts.append(current)
            current, bones, textures = [], set(), set()
        current.append((model, plan))
        bones |= model_bones
        textures |= model_tex
    if current:
        parts.append(current)
    return parts


__all__ = ["PlayerBudget", "TEXTURE_BUDGET", "split_player_parts"]
