"""Side-by-side frames of a view model and its retarget, seen as the player
sees them: the eye at the origin, forward -Y, up +Z in SMD space (studiomdl
turns SMD data +90 deg about Z), a 16:9 view with a ~106 deg horizontal field
— so an arm end or a shoulder showing on screen shows here too. A small
software rasteriser (:mod:`.render`): no GPU, usable anywhere."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from valve_qc_merger.preview import render as R
from valve_qc_merger.studio.scene import ModelScene, build_scene

HALF_FOV_H = math.radians(53.0)
BACKGROUND = np.array([60, 70, 80], dtype=np.float64)
NEAR = 2.0


def view_image(scene: ModelScene, sequence: int, frame: float, width: int = 320,
               height: int = 180, choice: dict[str, int] | None = None) -> np.ndarray:
    """(height, width, 3) uint8: ``scene`` at ``frame`` of ``sequence``."""
    rot, trans = scene.world(*scene.local_pose(sequence, frame))
    batches = [b for b in scene.visible_batches(choice or {}) if len(b.bones)]
    image = np.empty((height, width, 3), np.uint8)
    image[:] = BACKGROUND.astype(np.uint8)
    if not batches:
        return image
    materials = sorted({b.material for b in batches})
    slot = {m: i for i, m in enumerate(materials)}
    positions, normals, uvs, material = [], [], [], []
    for batch in batches:
        pos, nrm = scene.skin(batch, rot, trans)
        positions.append(pos.reshape(-1, 3, 3))
        normals.append(nrm.reshape(-1, 3, 3))
        uvs.append(batch.uv.reshape(-1, 3, 2))
        material.append(np.full(len(pos) // 3, slot[batch.material]))
    world = np.concatenate(positions)
    # right = +X, up = +Z, forward = -Y (the engine's +X-forward view, turned back)
    camera = np.stack([world[..., 0], world[..., 2], -world[..., 1]], -1)
    keep = (camera[..., 2] > NEAR).all(-1)
    depth = camera[..., 2]
    focal = (width / 2) / math.tan(HALF_FOV_H)
    screen = np.stack([camera[..., 0] / np.maximum(depth, NEAR) * focal + width / 2,
                       -camera[..., 1] / np.maximum(depth, NEAR) * focal + height / 2,
                       depth], -1)
    nrm = np.concatenate(normals)
    nrm = np.stack([nrm[..., 0], nrm[..., 2], -nrm[..., 1]], -1)
    count = int(keep.sum())
    mesh = R._Mesh(screen[keep], nrm[keep], np.concatenate(uvs)[keep],
                   np.concatenate(material)[keep], np.zeros(count, bool), np.zeros(count, bool))
    textures = R._textures(scene, materials, [])
    masked = {slot[m] for m in materials if scene.render_modes.get(m.lower()) == "masked"}
    tri, bary, _depth = R._rasterise(mesh, np.arange(count), textures, width, height, masked)
    shaded = R._shade(mesh, textures, tri, bary)
    alpha = shaded[..., 3:4] / 255.0
    return (shaded[..., :3] * alpha + BACKGROUND * (1 - alpha)).astype(np.uint8)


def compare_sheet(original: Path, result: Path, sequence: str, frames: int = 5,
                  width: int = 320, height: int = 180) -> np.ndarray:
    """RGBA sheet: the original's frames on top, the retarget's below, at
    ``frames`` evenly spaced moments of ``sequence`` (a 4 px white rule
    between the rows)."""
    before, after = build_scene(Path(original)), build_scene(Path(result))
    rows = []
    for scene in (before, after):
        names = [s.name for s in scene.sequences]
        index = names.index(sequence)
        count = scene.sequences[index].frames
        moments = np.linspace(0, count - 1, frames) if frames > 1 else [0.0]
        rows.append(np.concatenate([view_image(scene, index, float(round(t)), width, height)
                                    for t in moments], 1))
    rule = np.full((4, rows[0].shape[1], 3), 255, np.uint8)
    rgb = np.concatenate([rows[0], rule, rows[1]], 0)
    return np.concatenate([rgb, np.full(rgb.shape[:2] + (1,), 255, np.uint8)], -1)


def sequences_in_both(original: Path, result: Path) -> list[str]:
    """Sequence names the two models share, in the original's order."""
    before, after = build_scene(Path(original)), build_scene(Path(result))
    theirs = {s.name for s in after.sequences}
    return [s.name for s in before.sequences if s.name in theirs]


__all__ = ["compare_sheet", "sequences_in_both", "view_image"]
