"""Split one merge group into compile-safe parts.

Each output part is one ``.mdl``: a skin bodygroup of body submodels sharing the
donor rig and animations. The binding limits are the compiler's submodels
(``--submodel-limit``; by default the run's ``limits.submodels()``: 32 for
stock studiomdl, 1024 for ours), the texture budget (count) and ~14 MB of
texture data (a .mdl stays under 16 MB). Bones are always the donor's (~55 <
127).
"""

from __future__ import annotations

from valve_qc_merger import limits
from valve_qc_merger.merge_players.discovery import PlayerModel

DEFAULT_SUBMODEL_LIMIT = 32  # stock studiomdl MAXSTUDIOMODELS per bodypart
TEXTURE_BUDGET = 80  # studiomdl degrades past ~80; hard engine cap is 100


def _materials(model: PlayerModel) -> set[str]:
    return {t.material.lower() for smd in model.body_meshes for t in smd.triangles}


def _texture_keys(model: PlayerModel) -> dict[tuple[str, str], int]:
    """(material, content digest) -> bytes in a .mdl, for every texture the
    body uses. Keyed by content: CSO bodies reuse names (``face.bmp``) for
    different images, which the merge stages as separate textures."""
    import hashlib

    from valve_qc_merger.merge_view.merger import _find_texture
    out: dict[tuple[str, str], int] = {}
    for material in _materials(model):
        found = _find_texture(model.directory, material)
        if found is None:
            out[(material, "")] = 0
            continue
        digest = hashlib.md5(found.read_bytes()).hexdigest()
        out[(material, digest)] = limits.texture_bytes(found)
    return out


def split_parts(
    models: list[PlayerModel],
    *,
    submodel_limit: int | None = None,
    texture_budget: int = TEXTURE_BUDGET,
    max_skins: int | None = None,
    reserve_submodels: int = 0,
) -> list[list[PlayerModel]]:
    """Greedy in-order packing under submodel / texture / max-skins budgets.

    Total submodels of a part = ``skins`` (body0, no blank) plus, for each extra
    part slot, ``1 (blank) + skins that own a part there``. ``reserve_submodels``
    (1 with ``--include-base``) counts the single-part donor skin in body0.
    """
    if submodel_limit is None:
        submodel_limit = limits.submodels()
    parts: list[list[PlayerModel]] = []
    current: list[PlayerModel] = []
    textures: set[tuple[str, str]] = set()
    sizes: dict[tuple[str, str], int] = {}  # (material, digest) -> bytes in the .mdl
    meshes = 0  # the part's mesh bytes (estimated)
    for model in models:
        reserve = reserve_submodels if not parts else 0
        cand = [*current, model]
        slots = max(len(m.body_meshes) for m in cand)
        submodels = len(cand) + reserve + sum(
            1 + sum(1 for m in cand if len(m.body_meshes) > k)
            for k in range(1, slots)
        )
        keys = _texture_keys(model)
        sizes.update(keys)
        mats = set(keys)
        mesh = limits.mesh_bytes(model.body_meshes)
        over_sub = bool(current) and submodels > submodel_limit
        over_tex = bool(current) and (
            len(textures | mats) > texture_budget
            or sum(sizes[m] for m in textures | mats) + meshes + mesh > limits.PART_BYTES)
        over_skins = bool(current) and max_skins is not None and len(cand) > max_skins
        if current and (over_sub or over_tex or over_skins):
            parts.append(current)
            current, textures, meshes = [], set(), 0
        current.append(model)
        textures |= mats
        meshes += mesh
    if current:
        parts.append(current)
    return parts


__all__ = ["split_parts", "DEFAULT_SUBMODEL_LIMIT", "TEXTURE_BUDGET"]
