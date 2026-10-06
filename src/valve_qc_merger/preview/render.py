"""Weapon preview images from view models, drawn without the hands.

A v_ model is the best-looking source a server has: every weapon has one
(knives have no w_ model), and it is far more detailed than the p_ model.
The hands go first — by bodygroup (``hands``/``arms``), by texture name
(``hand``, ``glove``, ``sleeve``, ``arm``…: CSO and Valve hands always say
so), else by bone (finger/hand/arm bones) — and the weapon is posed on the
first frame of its idle, turned to show its right side with the barrel
horizontal, and drawn by a small software rasteriser: no GPU, the same
result on every machine, usable from the CLI and in tests. When removing the
hands leaves (almost) nothing — a gauntlet *is* the hands — the preview is
drawn with them and says so.
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from valve_qc_merger.studio.model_info import texture_rgba
from valve_qc_merger.studio.scene import Batch, ModelScene, build_scene

_HAND_GROUP = re.compile(r"(?i)hand|arm|glove|sleeve")
_HAND_TEXTURE = re.compile(r"(?i)hand|glove|sleeve|(?:^|[^a-z])arms?(?:[^a-z]|$)|skin_?[lr]|"
                           r"finger")
_HAND_BONE = re.compile(r"(?i)finger|hand|forearm|upperarm|clavicle|wrist|thumb|palm|"
                        r"(?:^|[ _.])(?:l|r)[ _.]arm|(?:^|[ _.])arm[ _.]?[lr]?$")
_WEAPON_BONE = re.compile(r"(?i)gun|weapon|wpn|bullet|mag|clip|slide|barrel|blade|knife|"
                          r"shell|grenade|bomb|scope|sight")
MIN_WEAPON_SHARE = 0.08  # of the triangles, after bones; less: the weapon *is* the hands
# triangle counts of the stock CSO hand meshes (male/female, high/low): how a
# decompile that lost every name (body0_0, texture0.bmp) still gives them away
CSO_HAND_TRIANGLES = frozenset({1956, 1968, 1992, 2100, 2140, 2160, 2272})


@dataclass
class Options:
    width: int = 512
    height: int = 256
    supersample: int = 3
    padding: float = 0.06  # of the canvas, each side
    barrel_left: bool = False  # mirror: the muzzle points left
    keep_hands: bool = False
    sequence: str = ""  # "" = the first idle (else the first sequence)
    frame: int = 0


@dataclass
class Preview:
    name: str
    rgba: np.ndarray  # (H, W, 4) uint8, transparent background
    hands: str  # how the hands went: bodygroup, texture, mesh, bones, kept, none
    triangles: int
    warnings: list[str] = field(default_factory=list)

    def png(self) -> bytes:
        return encode_png(self.rgba)


# --------------------------------------------------------------------------- #
# picking the weapon
# --------------------------------------------------------------------------- #
def _default_choice(scene: ModelScene) -> dict[str, int]:
    """Entry 0 of every bodygroup, except a hands group: its blank if it has
    one (the merged models' hands toggle)."""
    choice = {}
    for group, entries in scene.groups.items():
        if _HAND_GROUP.search(group) and "blank" in entries:
            choice[group] = entries.index("blank")
    return choice


def _arm_bones(scene: ModelScene) -> np.ndarray:
    """Bones of the arms: named like hand/finger/arm bones, their children
    (fingers named ``Bone27``) unless named like a weapon part, and parents
    whose every child is an arm bone (an upper arm called ``Bone01``)."""
    count = len(scene.bone_names)
    arm = [bool(_HAND_BONE.search(n)) for n in scene.bone_names]
    children: list[list[int]] = [[] for _ in range(count)]
    for bone, parent in enumerate(scene.parents):
        if parent >= 0:
            children[int(parent)].append(bone)
    for bone in scene.order:  # parents first: hand -> fingers
        parent = int(scene.parents[bone])
        if parent >= 0 and arm[parent] and not _WEAPON_BONE.search(scene.bone_names[bone]):
            arm[bone] = True
    for bone in reversed(scene.order):  # children first: forearm -> upper arm
        if not arm[bone] and children[bone] and all(arm[c] for c in children[bone]) \
                and scene.parents[bone] >= 0:
            arm[bone] = True
    return np.array(arm, bool)


def weapon_triangles(scene: ModelScene, keep_hands: bool = False
                     ) -> tuple[list[tuple[Batch, np.ndarray]], str]:
    """(batch, triangle indices into it) to draw, and how the hands went."""
    batches = [b for b in scene.visible_batches(_default_choice(scene)) if len(b.bones) >= 3]
    every = [(b, np.arange(len(b.bones) // 3)) for b in batches]
    total = sum(len(t) for _b, t in every)
    if keep_hands or not total:
        return every, "kept" if keep_hands else "none"

    def share(kept: list[tuple[Batch, np.ndarray]]) -> float:
        return sum(len(t) for _b, t in kept) / total

    hand_stems = {s for g, entries in scene.groups.items() if _HAND_GROUP.search(g)
                  for s in entries}
    stems = {b.stem for b in batches}
    if stems & hand_stems:
        kept = [(b, t) for b, t in every if b.stem not in hand_stems]
        if share(kept) >= 0.01:
            return kept, "bodygroup"
    kept = [(b, t) for b, t in every if not _HAND_TEXTURE.search(Path(b.material).stem)]
    if not kept:  # every texture is a hand: zombie claws, a gauntlet
        return every, "kept"
    if len(kept) < len(every) and share(kept) >= 0.01:
        return kept, "texture"
    per_stem: dict[str, list[tuple[Batch, np.ndarray]]] = {}
    for batch, tris in every:
        per_stem.setdefault(batch.stem, []).append((batch, tris))
    known = {stem for stem, members in per_stem.items()
             if sum(len(t) for _b, t in members) in CSO_HAND_TRIANGLES
             and len({int(i) for b, _t in members for i in np.unique(b.bones)}) >= 20}
    if known and len(known) < len(per_stem):
        kept = [(b, t) for b, t in every if b.stem not in known]
        if share(kept) >= 0.01:
            return kept, "mesh"
    arm = _arm_bones(scene)
    if arm.any():
        kept = []
        for batch, tris in every:
            on_arm = arm[batch.bones].reshape(-1, 3).any(axis=1)[tris]
            if (~on_arm).any():
                kept.append((batch, tris[~on_arm]))
        if share(kept) >= MIN_WEAPON_SHARE:
            return kept, "bones"
    return every, "kept"


def _main_cluster(parts: list[tuple[Batch, np.ndarray]], positions: list[np.ndarray],
                  gap: float = 0.08, share: float = 0.4
                  ) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Per part: a triangle mask without the pieces an idle hides away from
    the weapon (a speed loader parked under the camera, a spare magazine).
    Triangles are grouped by bone, groups closer than ``gap`` x the whole
    size join one cluster; the largest cluster stays, and so does any other
    with at least ``share`` of its triangles (the second gun of a pair).
    A handful of triangles spanning far more than everything else (the
    giant anti-rip planes some CSO models hide off-screen) goes first.
    Returns the masks and those of the largest cluster alone (one gun of a
    pair: what the camera is aimed by)."""
    keys: list[tuple[int, int]] = []
    boxes, sizes = [], []
    for part, (batch, tris) in enumerate(parts):
        first = batch.bones[tris * 3]
        for bone in np.unique(first):
            mine = tris[first == bone]
            points = positions[part][(mine[:, None] * 3 + np.arange(3)).reshape(-1)]
            keys.append((part, int(bone)))
            boxes.append((points.min(0), points.max(0)))
            sizes.append(len(mine))
    if not keys:
        empty = [np.zeros(len(t), bool) for _b, t in parts]
        return empty, empty
    total = sum(sizes)
    diagonals = np.array([np.linalg.norm(hi - lo) for lo, hi in boxes])
    junk = set()
    for i, size in enumerate(sizes):
        rest = [j for j in range(len(keys)) if j != i and diagonals[j] > 0]
        if size <= max(16, 0.01 * total) and rest:
            lo = np.min([boxes[j][0] for j in rest], axis=0)
            hi = np.max([boxes[j][1] for j in rest], axis=0)
            if diagonals[i] > 2.0 * np.linalg.norm(hi - lo):
                junk.add(i)
    live = [i for i in range(len(keys)) if i not in junk] or list(range(len(keys)))
    every = np.array([b for i in live for b in boxes[i]])
    reach = gap * float(np.linalg.norm(every.max(0) - every.min(0)))
    label = list(range(len(keys)))  # union-find over near boxes

    def root(i: int) -> int:
        while label[i] != i:
            label[i] = label[label[i]]
            i = label[i]
        return i

    for a, i in enumerate(live):
        alo, ahi = boxes[i]
        for j in live[a + 1:]:
            blo, bhi = boxes[j]
            if np.linalg.norm(np.maximum(0, np.maximum(blo - ahi, alo - bhi))) <= reach:
                label[root(i)] = root(j)
    clusters: dict[int, int] = {}
    for i in live:
        clusters[root(i)] = clusters.get(root(i), 0) + sizes[i]
    biggest = max(clusters.values())
    largest = max(clusters, key=clusters.__getitem__)
    kept = {keys[i] for i in live if clusters[root(i)] >= share * biggest}
    main = {keys[i] for i in live if root(i) == largest}

    def masks_of(chosen: set[tuple[int, int]]) -> list[np.ndarray]:
        return [np.isin(batch.bones[tris * 3], [bone for p, bone in chosen if p == part])
                for part, (batch, tris) in enumerate(parts)]

    return masks_of(kept), masks_of(main)


def _sequence(scene: ModelScene, name: str) -> int | None:
    if not scene.sequences:
        return None
    names = [s.name.lower() for s in scene.sequences]
    if name and name.lower() in names:
        return names.index(name.lower())
    return next((i for i, n in enumerate(names) if n.startswith("idle")), 0)


# --------------------------------------------------------------------------- #
# camera
# --------------------------------------------------------------------------- #
def _rotate(right: np.ndarray, up: np.ndarray, angle: float) -> tuple[np.ndarray, np.ndarray]:
    c, s = np.cos(angle), np.sin(angle)
    return right * c + up * s, up * c - right * s


def surface_samples(triangles: np.ndarray, per_edge: int = 8) -> np.ndarray:
    """Points spread over (T,3,3) ``triangles`` (a barycentric grid): a long
    barrel is two triangles, its middle has no vertex to measure."""
    steps = np.arange(per_edge + 1) / per_edge
    u, v = np.meshgrid(steps, steps)
    keep = (u + v) <= 1.0 + 1e-9
    u, v = u[keep], v[keep]
    w = 1.0 - u - v
    a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
    return (a[:, None] * w[None, :, None] + b[:, None] * u[None, :, None]
            + c[:, None] * v[None, :, None]).reshape(-1, 3)


def side_view(points: np.ndarray, triangles: np.ndarray | None = None) -> np.ndarray:
    """Camera basis rows (right, up, view) showing the weapon's right side:
    view along its thinnest axis, the muzzle to the right, up towards +Z,
    turned so the barrel (the top edge of the front) is level. Points are in the
    decompiled SMD frame, where a view model looks down -Y (studiomdl turns
    it by 90 degrees into the engine's +X)."""
    centered = points - points.mean(axis=0)
    values, vectors = np.linalg.eigh(np.cov(centered.T))  # ascending
    # a flat weapon (a blade, a gun held tilted) is seen across its thinnest
    # axis; anything else (a pair of pistols side by side, a grenade) from
    # the principal axis nearest the sideways one
    if values[1] > 0 and values[0] / values[1] < 0.3:
        normal = vectors[:, 0]
    else:
        normal = vectors[:, int(np.argmax(np.abs(vectors[0])))]
    forward = np.array([0.0, -1.0, 0.0])
    right = forward - normal * (forward @ normal)
    if np.linalg.norm(right) < 0.3:  # pointing at the camera: use the long axis
        right = vectors[:, 2] * (-np.sign(vectors[1, 2]) or 1.0)
        right = right - normal * (right @ normal)
    right /= np.linalg.norm(right)
    view = normal
    up = np.cross(right, view)
    if up[2] < 0:
        view, up = -view, -up
    # lay a weapon held upright (a knife, a hammer) down: its long axis
    # horizontal, the end that pointed up to the right
    x, y = centered @ right, centered @ up
    spread, axes = np.linalg.eigh(np.cov(np.stack([x, y])))
    long_axis = axes[:, 1] if axes[1, 1] >= 0 else -axes[:, 1]  # towards up
    tilt = np.arctan2(long_axis[1], long_axis[0])  # 0..180 degrees
    if spread[1] > 2.0 * spread[0] and np.radians(45) < tilt < np.radians(135):
        right, up = _rotate(right, up, tilt)
    surface = (surface_samples(triangles) - points.mean(axis=0)) if triangles is not None \
        and len(triangles) else centered
    turned = 0.0
    for _pass in range(3):  # the front moves as it turns: settle in a few steps
        angle = barrel_tilt(surface @ right, surface @ up)
        angle = float(np.clip(turned + angle, -np.radians(35), np.radians(35))) - turned
        if abs(angle) < np.radians(0.5):
            break
        right, up = _rotate(right, up, angle)
        turned += angle
    return np.stack([right, up, view])


def barrel_tilt(x: np.ndarray, y: np.ndarray, front: float = 0.5,
                limit: float = 35.0) -> float:
    """The angle (radians) that levels the barrel: the centre line of the
    thinnest columns of the front ``front`` of an elongated outline (a
    magazine, a grip or a sight makes its columns thick and is left out), as
    the median of the pairwise slopes — robust to the odd part. 0 for a
    round outline (a grenade) or a front too sparse to tell; never more than
    ``limit`` degrees."""
    if len(x) < 8:
        return 0.0
    spread = np.linalg.eigvalsh(np.cov(np.stack([x, y])))
    if spread[0] <= 0 or spread[1] < 2.0 * spread[0]:
        return 0.0
    span = float(np.ptp(x))
    start = float(x.max()) - front * span
    mask = x >= start
    count = 32
    bins = np.floor((x[mask] - start) / (front * span) * count).clip(0, count - 1)
    bins = bins.astype(int)
    columns, middles, thickness = [], [], []
    for b in range(count):
        hit = bins == b
        if hit.any():
            column = y[mask][hit]
            columns.append(start + (b + 0.5) * front * span / count)
            middles.append(float(column.max() + column.min()) / 2)
            thickness.append(float(np.ptp(column)))
    if len(columns) < 6:
        return 0.0
    thin = np.array(thickness) <= np.percentile(thickness, 50)
    cx, cy = np.array(columns)[thin], np.array(middles)[thin]
    if len(cx) < 4:
        return 0.0
    i, j = np.triu_indices(len(cx), k=1)
    slopes = (cy[j] - cy[i]) / (cx[j] - cx[i])
    angle = float(np.arctan(np.median(slopes)))
    return float(np.clip(angle, -np.radians(limit), np.radians(limit)))


# --------------------------------------------------------------------------- #
# rasteriser
# --------------------------------------------------------------------------- #
@dataclass
class _Mesh:
    screen: np.ndarray  # (T,3,3) pixel x, pixel y, depth
    normals: np.ndarray  # (T,3,3) camera space
    uv: np.ndarray  # (T,3,2)
    material: np.ndarray  # (T,) index into textures
    fullbright: np.ndarray  # (T,) bool
    additive: np.ndarray  # (T,) bool: glows, added over the rest


def _textures(scene: ModelScene, materials: list[str], warnings: list[str]
              ) -> list[np.ndarray]:
    out = []
    for material in materials:
        path = scene.textures.get(material)
        image = None
        if path is not None:
            try:
                masked = scene.render_modes.get(material.lower()) == "masked"
                width, height, rgba = texture_rgba(path, masked=masked)
                image = np.frombuffer(rgba, np.uint8).reshape(height, width, 4)
            except Exception as exc:  # noqa: BLE001 - an unreadable BMP draws grey
                warnings.append(f"texture {material}: {exc}")
        if image is None:
            image = np.full((1, 1, 4), 180, np.uint8)
            image[..., 3] = 255
        out.append(image)
    return out


def _sample(texture: np.ndarray, uv: np.ndarray) -> np.ndarray:
    height, width = texture.shape[:2]
    x = (np.floor(np.mod(uv[:, 0], 1.0) * width).astype(int)) % width
    y = (np.floor(np.mod(1.0 - uv[:, 1], 1.0) * height).astype(int)) % height
    return texture[y, x]


def _rasterise(mesh: _Mesh, indices: np.ndarray, textures: list[np.ndarray], width: int,
               height: int, masked: set[int], depth: np.ndarray | None = None
               ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per pixel: triangle index (-1 = empty) and barycentrics (b1, b2) of
    the nearest of ``indices``, behind nothing nearer in ``depth``."""
    depth = np.full((height, width), np.inf) if depth is None else depth.copy()
    tri = np.full((height, width), -1, np.int32)
    bary = np.zeros((height, width, 2))
    for index in indices:
        a, b, c = mesh.screen[index]
        x0 = max(int(np.floor(min(a[0], b[0], c[0]))), 0)
        x1 = min(int(np.ceil(max(a[0], b[0], c[0]))), width - 1)
        y0 = max(int(np.floor(min(a[1], b[1], c[1]))), 0)
        y1 = min(int(np.ceil(max(a[1], b[1], c[1]))), height - 1)
        if x0 > x1 or y0 > y1:
            continue
        area = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(area) < 1e-12:
            continue
        px, py = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        w1 = ((px - a[0]) * (c[1] - a[1]) - (py - a[1]) * (c[0] - a[0])) / area
        w2 = ((b[0] - a[0]) * (py - a[1]) - (b[1] - a[1]) * (px - a[0])) / area
        w0 = 1.0 - w1 - w2
        inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not inside.any():
            continue
        z = w0 * a[2] + w1 * b[2] + w2 * c[2]
        window = depth[y0:y1 + 1, x0:x1 + 1]
        hit = inside & (z < window)
        if mesh.material[index] in masked and hit.any():
            uv = (w0[hit, None] * mesh.uv[index, 0] + w1[hit, None] * mesh.uv[index, 1]
                  + w2[hit, None] * mesh.uv[index, 2])
            alpha = _sample(textures[mesh.material[index]], uv)[:, 3]
            keep = np.zeros_like(hit)
            keep[hit] = alpha > 0
            hit = keep
        window[hit] = z[hit]
        tri[y0:y1 + 1, x0:x1 + 1][hit] = index
        bary[y0:y1 + 1, x0:x1 + 1][hit] = np.stack([w1[hit], w2[hit]], axis=1)
    return tri, bary, depth


def _shade(mesh: _Mesh, textures: list[np.ndarray], tri: np.ndarray,
           bary: np.ndarray) -> np.ndarray:
    height, width = tri.shape
    out = np.zeros((height, width, 4), np.float64)
    covered = tri >= 0
    t = tri[covered]
    w1, w2 = bary[covered, 0:1], bary[covered, 1:2]
    w0 = 1.0 - w1 - w2
    uv = w0 * mesh.uv[t, 0] + w1 * mesh.uv[t, 1] + w2 * mesh.uv[t, 2]
    normal = w0 * mesh.normals[t, 0] + w1 * mesh.normals[t, 1] + w2 * mesh.normals[t, 2]
    normal /= np.maximum(np.linalg.norm(normal, axis=1, keepdims=True), 1e-9)
    normal[normal[:, 2] > 0] *= -1  # two-sided: face the camera (view is +depth)
    light = np.array([-0.35, 0.55, -0.75])
    light /= np.linalg.norm(light)
    fill = np.array([0.6, -0.2, -0.5])
    fill /= np.linalg.norm(fill)
    shade = 0.42 + 0.68 * np.clip(normal @ light, 0, None) + 0.18 * np.clip(normal @ fill, 0,
                                                                            None)
    shade = np.where(mesh.fullbright[t] | mesh.additive[t], 1.0, shade)
    color = np.zeros((len(t), 3))
    for material in np.unique(mesh.material[t]):
        rows = mesh.material[t] == material
        color[rows] = _sample(textures[material], uv[rows])[:, :3]
    pixels = np.clip(color * shade[:, None], 0, 255)
    out[covered, :3] = pixels
    out[covered, 3] = 255.0
    return out


def _downsample(image: np.ndarray, factor: int) -> np.ndarray:
    if factor == 1:
        return np.clip(image, 0, 255).astype(np.uint8)
    height, width = image.shape[0] // factor, image.shape[1] // factor
    alpha = image[..., 3:4] / 255.0
    premultiplied = np.concatenate([image[..., :3] * alpha, image[..., 3:4]], axis=2)
    blocks = premultiplied.reshape(height, factor, width, factor, 4).mean(axis=(1, 3))
    a = blocks[..., 3:4] / 255.0
    rgb = np.where(a > 0, blocks[..., :3] / np.maximum(a, 1e-9), 0)
    return np.clip(np.concatenate([rgb, blocks[..., 3:4]], axis=2) + 0.5, 0, 255).astype(np.uint8)


def render_scene(scene: ModelScene, options: Options | None = None) -> Preview:
    """The preview of a loaded scene (see the module docstring)."""
    options = options or Options()
    warnings: list[str] = []
    parts, hands = weapon_triangles(scene, options.keep_hands)
    if hands == "kept" and not options.keep_hands:
        warnings.append("no hands found to leave out (or the weapon is the hands): drawn "
                        "as it is")
    rot, trans = scene.world(*scene.local_pose(_sequence(scene, options.sequence),
                                               options.frame))
    materials = sorted({b.material for b, _t in parts})
    slot = {m: i for i, m in enumerate(materials)}
    skinned = [scene.skin(batch, rot, trans) for batch, _tris in parts]
    masks, aim = _main_cluster(parts, [pos for pos, _nrm in skinned])
    aim_points = np.concatenate(
        [pos[(tris[mask][:, None] * 3 + np.arange(3)).reshape(-1)]
         for (_batch, tris), mask, (pos, _nrm) in zip(parts, aim, skinned, strict=True)])
    dropped = sum(int((~m).sum()) for m in masks)
    if dropped:
        warnings.append(f"left out {dropped} triangle(s) the pose keeps away from the weapon")
    parts = [(batch, tris[mask]) for (batch, tris), mask in zip(parts, masks, strict=True)]
    positions, normals, uvs, material, fullbright, additive = [], [], [], [], [], []
    for (batch, tris), (pos, nrm) in zip(parts, skinned, strict=True):
        corners = (tris[:, None] * 3 + np.arange(3)).reshape(-1)
        positions.append(pos[corners].reshape(-1, 3, 3))
        normals.append(nrm[corners].reshape(-1, 3, 3))
        uvs.append(batch.uv[corners].reshape(-1, 3, 2))
        material.append(np.full(len(tris), slot[batch.material]))
        fullbright.append(np.full(len(tris), batch.render_mode == "fullbright"))
        additive.append(np.full(len(tris), batch.render_mode == "additive"))
    width, height = options.width, options.height
    if not positions:
        return Preview(scene.name, np.zeros((height, width, 4), np.uint8), hands, 0,
                       warnings + ["nothing to draw"])
    world = np.concatenate(positions)
    if len(aim_points) >= 3:  # corners come in threes: the aim cluster's triangles
        basis = side_view(aim_points, aim_points.reshape(-1, 3, 3))
    else:
        basis = side_view(world.reshape(-1, 3), world)
    camera = world @ basis.T  # (T,3,3): right, up, depth
    factor = max(int(options.supersample), 1)
    big_w, big_h = width * factor, height * factor
    pad_x, pad_y = big_w * options.padding, big_h * options.padding
    lo = camera[..., :2].reshape(-1, 2).min(axis=0)
    hi = camera[..., :2].reshape(-1, 2).max(axis=0)
    span = np.maximum(hi - lo, 1e-6)
    scale = min((big_w - 2 * pad_x) / span[0], (big_h - 2 * pad_y) / span[1])
    offset = np.array([big_w, big_h]) / 2 - np.array([(lo[0] + hi[0]) / 2 * scale,
                                                     -(lo[1] + hi[1]) / 2 * scale])
    screen = np.empty_like(camera)
    screen[..., 0] = camera[..., 0] * scale + offset[0]
    screen[..., 1] = -camera[..., 1] * scale + offset[1]
    screen[..., 2] = camera[..., 2]
    mesh = _Mesh(screen, np.concatenate(normals) @ basis.T, np.concatenate(uvs),
                 np.concatenate(material), np.concatenate(fullbright),
                 np.concatenate(additive))
    textures = _textures(scene, materials, warnings)
    masked = {slot[m] for m in materials if scene.render_modes.get(m.lower()) == "masked"}
    solid = np.flatnonzero(~mesh.additive)
    tri, bary, depth = _rasterise(mesh, solid, textures, big_w, big_h, masked)
    frame = _shade(mesh, textures, tri, bary)
    glows = np.flatnonzero(mesh.additive)
    if len(glows):
        # GoldSource draws additive meshes after the rest: unlit, adding up
        tri, bary, _depth = _rasterise(mesh, glows, textures, big_w, big_h, masked,
                                       depth + 0.02)
        glow = _shade(mesh, textures, tri, bary)[..., :3]
        frame[..., :3] = np.clip(frame[..., :3] + glow, 0, 255)
        frame[..., 3] = np.maximum(frame[..., 3], glow.max(axis=2))
    image = _downsample(frame, factor)
    if options.barrel_left:
        image = image[:, ::-1].copy()
    return Preview(scene.name, image, hands, len(mesh.screen), warnings)


def render_model(directory: Path, options: Options | None = None) -> Preview:
    """The preview of a decompiled model folder."""
    return render_scene(build_scene(Path(directory)), options)


# --------------------------------------------------------------------------- #
# PNG
# --------------------------------------------------------------------------- #
def encode_png(rgba: np.ndarray) -> bytes:
    """A truecolour + alpha PNG of an (H, W, 4) uint8 array."""
    height, width = rgba.shape[:2]
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def decode_png(data: bytes) -> np.ndarray:
    """The pixels of a PNG written by :func:`encode_png` (filter 0 rows only)."""
    pos, idat, size = 8, b"", (0, 0)
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            size = struct.unpack(">II", body[:8])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    width, height = size
    rows = np.frombuffer(zlib.decompress(idat), np.uint8).reshape(height, width * 4 + 1)
    return rows[:, 1:].reshape(height, width, 4).copy()


__all__ = ["Options", "Preview", "barrel_tilt", "decode_png", "encode_png", "render_model",
           "render_scene", "side_view", "weapon_triangles"]
