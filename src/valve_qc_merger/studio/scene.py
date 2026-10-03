"""CPU side of the viewport (Qt-free, numpy): skeleton, poses, skinning.

A :class:`ModelScene` is built once from a decompiled model. Every reference
mesh is split into *batches* (one per submodel and material) whose vertices
are stored in their bone's local frame (each SMD's own bind, as studiomdl
does). Posing a sequence frame gives bone world matrices; skinning a batch
is one ``einsum``. Frames are interpolated like the engine: positions
linearly, rotations by quaternion slerp.

All coordinates are SMD space (Z up). A GoldSource view model faces -Y in
SMD space (studiomdl's +90 deg turn makes that +X in game).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from valve_qc_merger.merge_player.merger import parse_texturegroups
from valve_qc_merger.merge_view.discovery import ModelInput, load_model
from valve_qc_merger.models.smd import Smd
from valve_qc_merger.studio.model_info import _entries_with_blanks

_RENDERMODE_SPLIT = ("masked", "additive")


# --------------------------------------------------------------------------- #
# Rotation helpers (GoldSource convention: R = Rz . Ry . Rx)
# --------------------------------------------------------------------------- #
def euler_to_quat(euler: np.ndarray) -> np.ndarray:
    """(N,3) radians -> (N,4) quaternions (x, y, z, w), HLSDK AngleQuaternion."""
    hx, hy, hz = (euler[:, 0] * 0.5, euler[:, 1] * 0.5, euler[:, 2] * 0.5)
    sx, cx = np.sin(hx), np.cos(hx)
    sy, cy = np.sin(hy), np.cos(hy)
    sz, cz = np.sin(hz), np.cos(hz)
    return np.stack([
        sx * cy * cz - cx * sy * sz,
        cx * sy * cz + sx * cy * sz,
        cx * cy * sz - sx * sy * cz,
        cx * cy * cz + sx * sy * sz,
    ], axis=1)


def quat_to_matrix(q: np.ndarray) -> np.ndarray:
    """(N,4) (x,y,z,w) -> (N,3,3)."""
    x, y, z, w = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    return np.stack([
        np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)], 1),
        np.stack([2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)], 1),
        np.stack([2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)], 1),
    ], axis=1)


def quat_slerp(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    dot = np.sum(a * b, axis=1, keepdims=True)
    b = np.where(dot < 0, -b, b)
    dot = np.abs(dot)
    near = dot > 0.9995
    theta = np.arccos(np.clip(dot, -1.0, 1.0))
    sin = np.sin(theta)
    sin = np.where(near, 1.0, sin)
    wa = np.where(near, 1 - t, np.sin((1 - t) * theta) / sin)
    wb = np.where(near, t, np.sin(t * theta) / sin)
    out = wa * a + wb * b
    return out / np.linalg.norm(out, axis=1, keepdims=True)


# --------------------------------------------------------------------------- #
# Scene
# --------------------------------------------------------------------------- #
@dataclass
class Batch:
    """Vertices of one submodel drawn with one texture (triangle list)."""

    stem: str
    material: str
    render_mode: str
    bones: np.ndarray  # (N,) global bone index
    local_pos: np.ndarray  # (N,3) in the bone's bind frame
    local_nrm: np.ndarray  # (N,3)
    uv: np.ndarray  # (N,2) SMD uv (v up)


@dataclass
class SequenceData:
    name: str
    fps: float
    loop: bool
    positions: np.ndarray  # (F, B, 3) local
    quats: np.ndarray  # (F, B, 4) local

    @property
    def frames(self) -> int:
        return self.positions.shape[0]


@dataclass
class ModelScene:
    name: str
    directory: Path
    bone_names: list[str]
    parents: np.ndarray  # (B,)
    order: list[int]  # parents before children
    bind_positions: np.ndarray  # (B,3) local, from the fullest mesh
    bind_quats: np.ndarray  # (B,4)
    groups: dict[str, list[str]]  # bodygroup -> entries ("blank" kept)
    batches: list[Batch]
    sequences: list[SequenceData]
    textures: dict[str, Path | None]  # material -> file
    render_modes: dict[str, str]
    attachments: list[tuple[int, int, np.ndarray]] = field(default_factory=list)
    # $texturegroup rows (row 0 = the textures the meshes use); [] = no skins
    skins: list[list[str]] = field(default_factory=list)

    def with_skin(self, index: int) -> ModelScene:
        """This scene drawn with skin row ``index``: every material of row 0
        swapped for the same column of that row (0 or out of range: self)."""
        if index <= 0 or index >= len(self.skins):
            return self
        mapping = {old.lower(): new for old, new in zip(self.skins[0], self.skins[index],
                                                         strict=False)}
        textures = dict(self.textures)
        batches = []
        for batch in self.batches:
            new = mapping.get(batch.material.lower())
            if new is None:
                batches.append(batch)
                continue
            if new not in textures:
                textures[new] = next(
                    (p for p in self.directory.rglob("*")
                     if p.is_file() and p.name.lower() == new.lower()), None)
            batches.append(replace(batch, material=new,
                                   render_mode=self.render_modes.get(new.lower(),
                                                                     batch.render_mode)))
        return replace(self, batches=batches, textures=textures)

    # -- posing ------------------------------------------------------------
    def local_pose(self, sequence: int | None, frame: float) -> tuple[np.ndarray, np.ndarray]:
        """Local (positions, quats) of every bone; ``None`` = bind pose."""
        if sequence is None or not self.sequences:
            return self.bind_positions, self.bind_quats
        seq = self.sequences[sequence]
        count = seq.frames
        if count == 1:
            return seq.positions[0], seq.quats[0]
        if seq.loop:
            frame %= count
        else:
            frame = min(max(frame, 0.0), count - 1)
        f0 = int(frame)
        f1 = (f0 + 1) % count if seq.loop else min(f0 + 1, count - 1)
        t = frame - f0
        pos = seq.positions[f0] * (1 - t) + seq.positions[f1] * t
        return pos, quat_slerp(seq.quats[f0], seq.quats[f1], t)

    def world(self, positions: np.ndarray, quats: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Bone world (rotations (B,3,3), translations (B,3)) by FK."""
        local_r = quat_to_matrix(quats)
        rot = np.empty_like(local_r)
        trans = np.empty_like(positions)
        for b in self.order:
            p = self.parents[b]
            if p < 0:
                rot[b] = local_r[b]
                trans[b] = positions[b]
            else:
                rot[b] = rot[p] @ local_r[b]
                trans[b] = rot[p] @ positions[b] + trans[p]
        return rot, trans

    def skin(self, batch: Batch, rot: np.ndarray,
             trans: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """World positions and normals of a batch for bone matrices."""
        r = rot[batch.bones]
        pos = np.einsum("nij,nj->ni", r, batch.local_pos) + trans[batch.bones]
        nrm = np.einsum("nij,nj->ni", r, batch.local_nrm)
        return pos, nrm

    def visible_batches(self, choice: dict[str, int]) -> list[Batch]:
        """Batches of the chosen entry of every bodygroup (default 0)."""
        stems = set()
        for group, entries in self.groups.items():
            index = min(max(choice.get(group, 0), 0), len(entries) - 1)
            stems.add(entries[index])
        return [b for b in self.batches if b.stem in stems]

    def decode_body(self, value: int) -> dict[str, int]:
        """``pev->body`` -> the entry chosen in every bodygroup (studiomdl
        numbers groups in QC order; each one's stride is the product of the
        sizes before it)."""
        choice: dict[str, int] = {}
        for group, entries in self.groups.items():
            count = max(len(entries), 1)
            choice[group] = value % count
            value //= count
        return choice

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        rot, trans = self.world(*self.local_pose(0 if self.sequences else None, 0))
        points = [self.skin(b, rot, trans)[0] for b in self.batches if len(b.bones)]
        if not points:
            return np.zeros(3), np.ones(3)
        allp = np.concatenate(points)
        return allp.min(0), allp.max(0)


def _smd_locals(smd: Smd, names: list[str],
                fallback: tuple[np.ndarray, np.ndarray]) -> list[tuple[np.ndarray, np.ndarray]]:
    """Per frame: local (positions (B,3), quats (B,4)) in the global bone order."""
    index_of = {n.index: names.index(n.name) for n in smd.nodes if n.name in names}
    out = []
    for frame in smd.frames:
        pos = fallback[0].copy()
        eul = np.zeros((len(names), 3))
        have = np.zeros(len(names), dtype=bool)
        for pose in frame.poses:
            g = index_of.get(pose.bone)
            if g is None:
                continue
            pos[g] = pose.position
            eul[g] = pose.rotation
            have[g] = True
        quats = euler_to_quat(eul)
        quats[~have] = fallback[1][~have]
        out.append((pos, quats))
    return out


def build_scene(directory: Path, model: ModelInput | None = None) -> ModelScene:
    """Load (or reuse) a decompiled model and prepare it for drawing."""
    model = model or load_model(Path(directory), require_anims=False)
    fullest = max(model.meshes.values(), key=lambda m: len(m.nodes))
    names = [n.name for n in fullest.nodes]
    by_index = {n.index: i for i, n in enumerate(fullest.nodes)}
    parents = np.array([by_index.get(n.parent, -1) if n.parent >= 0 else -1
                        for n in fullest.nodes])
    order: list[int] = []
    seen: set[int] = set()

    def visit(i: int) -> None:
        if i in seen:
            return
        if parents[i] >= 0:
            visit(int(parents[i]))
        seen.add(i)
        order.append(i)

    for i in range(len(names)):
        visit(i)
    zero = (np.zeros((len(names), 3)), np.tile([0.0, 0.0, 0.0, 1.0], (len(names), 1)))
    bind_pos, bind_quat = _smd_locals(fullest, names, zero)[0]

    from valve_qc_merger.studio.model_info import _RENDERMODE_RE  # noqa: PLC0415
    modes = {n.lower(): m for n, m in _RENDERMODE_RE.findall(model.qc_text)}
    scene = ModelScene(
        name=model.name, directory=model.directory, bone_names=names, parents=parents,
        order=order, bind_positions=bind_pos, bind_quats=bind_quat,
        groups=_entries_with_blanks(model.qc_text, model.bodygroups), batches=[],
        sequences=[], textures={}, render_modes=modes,
        skins=parse_texturegroups(model.qc_text),
    )

    for stem, mesh in model.meshes.items():
        if not mesh.triangles:
            continue
        mesh_names = {n.index: n.name for n in mesh.nodes}
        own = _smd_locals(mesh, names, (bind_pos, bind_quat))[0]
        rot, trans = scene.world(*own)
        per_material: dict[str, list] = {}
        for tri in mesh.triangles:
            per_material.setdefault(tri.material, []).append(tri)
        for material, tris in per_material.items():
            verts = [v for t in tris for v in t.vertices]
            bones = np.array([names.index(mesh_names[v.bone]) for v in verts])
            pos = np.array([tuple(v.position) for v in verts], dtype=np.float64)
            nrm = np.array([tuple(v.normal) for v in verts], dtype=np.float64)
            r = rot[bones]
            # bind-world -> bone-local: R^T (p - t)
            local_pos = np.einsum("nji,nj->ni", r, pos - trans[bones])
            local_nrm = np.einsum("nji,nj->ni", r, nrm)
            scene.batches.append(Batch(
                stem=stem, material=material,
                render_mode=modes.get(material.lower(), ""),
                bones=bones, local_pos=local_pos, local_nrm=local_nrm,
                uv=np.array([tuple(v.uv) for v in verts], dtype=np.float64),
            ))
            if material not in scene.textures:
                scene.textures[material] = next(
                    (p for p in model.directory.rglob("*")
                     if p.is_file() and p.name.lower() == material.lower()), None)

    for seq in model.sequences:
        anim = model.anims.get(seq.name)
        if anim is None or not anim.frames:
            continue
        frames = _smd_locals(anim, names, (bind_pos, bind_quat))
        scene.sequences.append(SequenceData(
            name=seq.name, fps=seq.fps or 30.0, loop=seq.loop,
            positions=np.stack([f[0] for f in frames]),
            quats=np.stack([f[1] for f in frames]),
        ))

    from valve_qc_merger.studio.model_info import _ATTACH_RE  # noqa: PLC0415
    for m in _ATTACH_RE.finditer(model.qc_text):
        bone = m.group(2) or m.group(3)
        if bone in names:
            scene.attachments.append((int(m.group(1)), names.index(bone),
                                      np.array([float(m.group(i)) for i in (4, 5, 6)])))
    return scene


__all__ = ["Batch", "ModelScene", "SequenceData", "build_scene", "euler_to_quat",
           "quat_slerp", "quat_to_matrix"]
