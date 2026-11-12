"""A map as a scene the studio's renderer draws: the world's and the brush
entities' faces, one batch per texture, the textures written as 8-bit BMPs
to a work folder; spawns as 32 x 32 x 72 boxes (CT blue, T red).

The renderer draws models: the map becomes one with a single bone at the
origin, its faces (triangle fans) in world units, the texture coordinates
the engine's (``s = v · vecs[0] + offset``, over the texture's width).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.geometry import geometry
from valve_qc_merger.mapkit.textures import SPECIAL, WadIndex, lump_entries
from valve_qc_merger.merge_view.bmp8 import Bmp8, write_bmp8
from valve_qc_merger.sprite.wad import MIPTEX, read_miptex
from valve_qc_merger.studio.scene import Batch, ModelScene, SequenceData

CT_BOX, T_BOX = "spawn_ct", "spawn_t"
LIGHT_LEVELS = 8  # the light of a face, rounded to one of these (a texture drawn darker)
LIGHT_GAMMA = 2.2  # the engine brightens lightmaps by a gamma curve (lightgamma 2.5)
LIGHT_SCALE = 1.6
LIGHT_MIN, LIGHT_MAX = 0.3, 1.9
BOX_COLOURS = {CT_BOX: (80, 150, 255), T_BOX: (255, 90, 80)}
HIDDEN = SPECIAL + ("aaatrigger", "trigger")


def _texture_files(bsp: B.Bsp, wads: list[WadIndex], folder: Path) -> dict[str, Path | None]:
    folder.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path | None] = {}
    entries = lump_entries(bsp)
    for tex in geometry(bsp).textures:
        if tex.offset < 0 or not tex.name:
            continue
        raw = entries[tex.index] if tex.embedded else next(
            (lump for w in wads if (lump := w.lump(tex.name)) is not None), None)
        if raw is None:
            out[tex.name] = None
            continue
        try:
            texture = read_miptex(raw, tex.name, MIPTEX)
        except (ValueError, IndexError):
            out[tex.name] = None
            continue
        palette = list(texture.palette)[:256] + [(0, 0, 0)] * max(0, 256 - len(texture.palette))
        path = folder / f"t{tex.index}.bmp"
        path.write_bytes(write_bmp8(Bmp8(texture.width, texture.height, palette,
                                         bytearray(texture.indices))))
        out[tex.name] = path
    for name, colour in BOX_COLOURS.items():
        path = folder / f"{name}.bmp"
        path.write_bytes(write_bmp8(Bmp8(1, 1, [colour] + [(0, 0, 0)] * 255, bytearray(1))))
        out[name] = path
    return out


def face_light(bsp: B.Bsp) -> np.ndarray:
    """The average light of every face from the map's lightmaps (0.3..1.9,
    1 = as the texture is; -1: no lightmap, drawn full bright)."""
    from valve_qc_merger.mapkit.geometry import TEX_SPECIAL
    geo = geometry(bsp)
    lighting = np.frombuffer(bsp.lumps[B.LIGHTING], np.uint8)
    out = np.full(len(geo.faces), -1.0)
    if not len(lighting) or not len(geo.faces):
        return out
    extents = geo.surface_extents()
    flags = geo.texinfo["flags"][np.minimum(geo.faces["texinfo"], len(geo.texinfo) - 1)]
    for face in range(len(geo.faces)):
        offset = int(geo.faces[face]["lightofs"])
        if offset < 0 or flags[face] & TEX_SPECIAL or geo.faces[face]["styles"][0] == 255:
            continue
        w, h = int(extents[face, 0]) // 16 + 1, int(extents[face, 1]) // 16 + 1
        samples = lighting[offset:offset + w * h * 3]
        if len(samples):
            value = (float(samples.mean()) / 255.0) ** (1.0 / LIGHT_GAMMA) * LIGHT_SCALE
            out[face] = min(max(value, LIGHT_MIN), LIGHT_MAX)
    return out


def _level(light: float) -> int:
    """A face's light -> one of ``LIGHT_LEVELS`` (the full-bright level when -1)."""
    if light < 0:
        return LIGHT_LEVELS
    return int(round((light - LIGHT_MIN) / (LIGHT_MAX - LIGHT_MIN) * (LIGHT_LEVELS - 1)))


def _level_factor(level: int) -> float:
    if level >= LIGHT_LEVELS:
        return 1.0
    return LIGHT_MIN + level / (LIGHT_LEVELS - 1) * (LIGHT_MAX - LIGHT_MIN)


def _lit_textures(textures: dict[str, Path | None], wanted: set[tuple[str, int]],
                  folder: Path) -> dict[str, Path | None]:
    """``<texture>@<level>`` BMPs: each texture's palette scaled by a light level."""
    from valve_qc_merger.merge_view.bmp8 import read_bmp8
    out: dict[str, Path | None] = {}
    for name, level in sorted(wanted):
        source = textures.get(name)
        key = f"{name}@{level}"
        if source is None:
            out[key] = None
            continue
        image = read_bmp8(source.read_bytes())
        factor = _level_factor(level)
        palette = [tuple(min(int(c * factor), 255) for c in rgb) for rgb in image.palette]
        path = folder / f"{source.stem}_{level}.bmp"
        path.write_bytes(write_bmp8(Bmp8(image.width, image.height, palette,  # type: ignore[arg-type]
                                         image.pixels)))
        out[key] = path
    return out


def _model_faces(bsp: B.Bsp, entities: list[dict[str, str]]) -> list[tuple[int, np.ndarray]]:
    """(face, offset) of the world and of every brush entity (at its origin)."""
    geo = geometry(bsp)
    out = [(int(f), np.zeros(3)) for f in geo.model_faces(0)]
    for entity in entities:
        model = entity.get("model", "")
        if not model.startswith("*"):
            continue
        try:
            index = int(model[1:])
            origin = np.array([float(v) for v in entity.get("origin", "0 0 0").split()[:3]])
        except ValueError:
            continue
        if 0 < index < len(geo.models) and origin.shape == (3,):
            out += [(int(f), origin) for f in geo.model_faces(index)]
    return out


def _box(origin: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lo = origin + [-16.0, -16.0, -36.0]
    hi = origin + [16.0, 16.0, 36.0]
    corners = np.array([[x, y, z] for z in (lo[2], hi[2]) for y in (lo[1], hi[1])
                        for x in (lo[0], hi[0])])
    quads = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    normals = [(0, 0, -1), (0, 0, 1), (0, -1, 0), (0, 1, 0), (-1, 0, 0), (1, 0, 0)]
    pos, nrm = [], []
    for quad, normal in zip(quads, normals, strict=True):
        a, b, c, d = (corners[i] for i in quad)
        pos += [a, b, c, a, c, d]
        nrm += [normal] * 6
    return np.array(pos), np.array(nrm, np.float64)


def map_scene(bsp: B.Bsp, wads: list[WadIndex], entities: list[dict[str, str]],
              work: Path, spawns: bool = True, light: bool = True) -> ModelScene:
    """The scene of ``bsp`` with ``entities`` (the planned ones); ``light``:
    every face as bright as its lightmap says (on average)."""
    geo = geometry(bsp)
    textures = _texture_files(bsp, wads, work)
    names = [t.name for t in geo.textures]
    normals = geo.face_normals()
    tex_of = geo.face_texture()
    lights = face_light(bsp) if light else np.full(len(geo.faces), -1.0)
    per_texture: dict[str, tuple[list[np.ndarray], list[np.ndarray], list[np.ndarray]]] = {}
    for face, offset in _model_faces(bsp, entities):
        t = int(tex_of[face])
        if not 0 <= t < len(names) or names[t].lower().startswith(HIDDEN):
            continue
        poly = geo.face_points(face).astype(np.float64)
        if len(poly) < 3:
            continue
        info = geo.texinfo[geo.faces[face]["texinfo"]]
        vecs = info["vecs"].astype(np.float64)
        tex = geo.textures[t]
        width, height = max(tex.width, 1), max(tex.height, 1)
        s = (poly @ vecs[0, :3] + vecs[0, 3]) / width
        tt = (poly @ vecs[1, :3] + vecs[1, 3]) / height
        uv = np.column_stack([s, 1.0 - tt])
        fan = np.array([[0, k, k + 1] for k in range(1, len(poly) - 1)]).ravel()
        material = f"{names[t]}@{_level(float(lights[face]))}" if light else names[t]
        pos, nrm, uvs = per_texture.setdefault(material, ([], [], []))
        pos.append(poly[fan] + offset)
        nrm.append(np.repeat(normals[face][None], len(fan), axis=0))
        uvs.append(uv[fan])
    if light:
        wanted = {(m.rsplit("@", 1)[0], int(m.rsplit("@", 1)[1])) for m in per_texture}
        textures.update(_lit_textures(textures, wanted, work))
    batches = []
    for name, (pos, nrm, uvs) in per_texture.items():
        points = np.concatenate(pos)
        batches.append(Batch("world", name, "masked" if name.startswith("{") else "normal",
                             np.zeros(len(points), np.int64), points, np.concatenate(nrm),
                             np.concatenate(uvs)))
    if spawns:
        for cls, material in (("info_player_start", CT_BOX), ("info_player_deathmatch", T_BOX)):
            boxes = []
            for entity in entities:
                if entity.get("classname") != cls:
                    continue
                try:
                    origin = np.array([float(v) for v in entity["origin"].split()[:3]])
                except (KeyError, ValueError):
                    continue
                boxes.append(_box(origin))
            if boxes:
                points = np.concatenate([b[0] for b in boxes])
                batches.append(Batch("spawns", material, "normal", np.zeros(len(points), np.int64),
                                     points, np.concatenate([b[1] for b in boxes]),
                                     np.zeros((len(points), 2))))
    identity = np.array([[0.0, 0.0, 0.0, 1.0]])
    still = SequenceData("map", 1.0, True, np.zeros((1, 1, 3)), identity[None])
    return ModelScene(
        name=bsp.name, directory=work, bone_names=["world"], parents=np.array([-1]), order=[0],
        bind_positions=np.zeros((1, 3)), bind_quats=identity,
        groups={"world": ["world"], "spawns": ["spawns"]}, batches=batches, sequences=[still],
        textures={k: v for k, v in textures.items()},
        render_modes={k.lower(): ("masked" if k.startswith("{") else "normal")
                      for k in textures})


__all__ = ["map_scene"]
