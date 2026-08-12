"""Split one merge group into compile-safe parts.

Each output part is one ``.mdl``: a skin bodygroup of body submodels sharing the
donor rig and animations. The binding limits are the per-bodypart submodel cap
(``--submodel-limit``, default 32 for stock studiomdl; raise it for a patched
compiler) and the texture budget. Bones are always the donor's (~55 < 127).
"""

from __future__ import annotations

from valve_qc_merger.merge_players.discovery import PlayerModel

DEFAULT_SUBMODEL_LIMIT = 32  # stock studiomdl MAXSTUDIOMODELS per bodypart
TEXTURE_BUDGET = 80  # studiomdl degrades past ~80; hard engine cap is 100


def _materials(model: PlayerModel) -> set[str]:
    return {t.material.lower() for smd in model.body_meshes for t in smd.triangles}


def split_parts(
    models: list[PlayerModel],
    *,
    submodel_limit: int = DEFAULT_SUBMODEL_LIMIT,
    texture_budget: int = TEXTURE_BUDGET,
    max_skins: int | None = None,
    reserve_submodels: int = 0,
) -> list[list[PlayerModel]]:
    """Greedy in-order packing under submodel / texture / max-skins budgets.

    Total submodels of a part = ``skins`` (body0, no blank) plus, for each extra
    part slot, ``1 (blank) + skins that own a part there``. ``reserve_submodels``
    (1 with ``--include-base``) counts the single-part donor skin in body0.
    """
    parts: list[list[PlayerModel]] = []
    current: list[PlayerModel] = []
    textures: set[str] = set()
    for model in models:
        reserve = reserve_submodels if not parts else 0
        cand = [*current, model]
        slots = max(len(m.body_meshes) for m in cand)
        submodels = len(cand) + reserve + sum(
            1 + sum(1 for m in cand if len(m.body_meshes) > k)
            for k in range(1, slots)
        )
        mats = _materials(model)
        over_sub = bool(current) and submodels > submodel_limit
        over_tex = bool(current) and len(textures | mats) > texture_budget
        over_skins = bool(current) and max_skins is not None and len(cand) > max_skins
        if current and (over_sub or over_tex or over_skins):
            parts.append(current)
            current, textures = [], set()
        current.append(model)
        textures |= mats
    if current:
        parts.append(current)
    return parts


__all__ = ["split_parts", "DEFAULT_SUBMODEL_LIMIT", "TEXTURE_BUDGET"]
