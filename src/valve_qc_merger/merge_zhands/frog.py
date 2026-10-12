"""One frog for every zombie: redraw a grenade animation on another frog rig.

CSO's zombie bombs are one frog asset (same triangles, same UVs) that every
zombie scaled and posed its own way — 13 rigs on the nexon set, the frog
x0.40 to x1.15. GoldSrc bones cannot scale, so a single frog mesh cannot
stand in for all of them as they are; instead each zombie's grenade
animation is redrawn for the ONE canonical frog:

- the frog's vertices are paired by UV (the same asset) and bone name;
- every frame, each canonical bone takes the rotation that best lays its
  part of the frog onto the zombie's own frog there (Kabsch, rotation only:
  the canonical frog keeps its size and shape — bone lengths stay, as
  GoldSrc needs);
- the whole frog is then moved so its centre sits where the zombie's frog
  is, in the claws, and hung from the zombie's own hand bone.

A frog with another UV layout cannot be paired: it keeps its own rig (the
merge gives it a grenade entry of its own).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from valve_qc_merger.merge_view.skeleton_ops import fk_worlds
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Smd
from valve_qc_merger.transform import Transform, matrix_to_euler

MIN_COVERAGE = 0.8  # of the canonical frog's vertices paired with the other frog
MIN_PAIRS = 3  # vertices a bone needs to take its own rotation


@dataclass
class Redraw:
    """What redrawing one zombie's grenade did."""

    sequences: int = 0
    frames: int = 0
    scale: float = 1.0  # their frog's size over the canonical frog's


def _matrix(xf: Transform) -> np.ndarray:
    m = np.eye(4)
    m[:3, :3] = np.array(xf.rotation, dtype=float)
    m[:3, 3] = (xf.translation.x, xf.translation.y, xf.translation.z)
    return m


def _rigid_inverse(m: np.ndarray) -> np.ndarray:
    out = np.eye(4)
    out[:3, :3] = m[:3, :3].T
    out[:3, 3] = -m[:3, :3].T @ m[:3, 3]
    return out


def _kabsch(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    """The rotation laying centred ``source`` points onto centred ``target``."""
    h = (source - source.mean(0)).T @ (target - target.mean(0))
    u, _s, vt = np.linalg.svd(h)
    d = np.sign(np.linalg.det(vt.T @ u.T)) or 1.0
    return vt.T @ np.diag([1.0, 1.0, d]) @ u.T


def _gren(smd: Smd, prefix: str) -> tuple[dict[str, str | None], list[str]]:
    """The grenade bones' parents (by name) and their order, parents first."""
    name_of = {n.index: n.name for n in smd.nodes}
    parent = {n.name: name_of.get(n.parent) for n in smd.nodes}
    bones = [n.name for n in smd.nodes if n.name.startswith(prefix)]

    def depth(bone: str) -> int:
        count = 0
        while parent.get(bone) is not None:
            bone, count = parent[bone], count + 1
        return count

    return {b: parent[b] for b in bones}, sorted(bones, key=depth)


def _bone_local_vertices(smd: Smd, prefix: str) -> dict[tuple, tuple[str, np.ndarray]]:
    """UV (+ bone) -> (bone, the vertex in its bone's bind frame), one per key."""
    worlds = fk_worlds(smd, smd.frames[0])
    inverse = {i: _rigid_inverse(_matrix(xf)) for i, xf in worlds.items()}
    name_of = {n.index: n.name for n in smd.nodes}
    out: dict[tuple, tuple[str, np.ndarray]] = {}
    for tri in smd.triangles:
        for v in tri.vertices:
            bone = name_of[v.bone]
            if not bone.startswith(prefix):
                continue
            key = (round(v.uv.u, 4), round(v.uv.v, 4), bone)
            if key not in out:
                local = inverse[v.bone] @ np.array([v.position.x, v.position.y, v.position.z, 1.0])
                out[key] = (bone, local[:3])
    return out


def can_redraw(canonical: Smd, theirs: Smd, prefix: str) -> bool:
    """Do the two frogs pair up (same grenade bones, most vertices by UV)?"""
    c_parent, _o = _gren(canonical, prefix)
    t_parent, _t = _gren(theirs, prefix)
    if set(c_parent) != set(t_parent):
        return False
    c_verts = _bone_local_vertices(canonical, prefix)
    t_verts = _bone_local_vertices(theirs, prefix)
    return bool(c_verts) and sum(k in t_verts for k in c_verts) / len(c_verts) >= MIN_COVERAGE


def redraw_grenade(canonical: Smd, theirs: Smd, anims: dict[str, Smd],
                   prefix: str) -> Redraw | None:
    """Rewrite the grenade bones of ``anims`` (one zombie's sequences, grenade
    bones named ``prefix…``) so ``canonical``'s frog follows ``theirs``.
    None (nothing changed) when the two frogs cannot be paired."""
    c_parent, c_order = _gren(canonical, prefix)
    t_parent, _t_order = _gren(theirs, prefix)
    if set(c_parent) != set(t_parent):
        return None
    c_verts = _bone_local_vertices(canonical, prefix)
    t_verts = _bone_local_vertices(theirs, prefix)
    pairs: dict[str, tuple[list[np.ndarray], list[np.ndarray]]] = {}
    for key, (bone, q) in c_verts.items():
        other = t_verts.get(key)
        if other is not None:
            pairs.setdefault(bone, ([], []))
            pairs[bone][0].append(q)
            pairs[bone][1].append(other[1])
    paired = sum(len(qs) for qs, _rs in pairs.values())
    if not c_verts or paired / len(c_verts) < MIN_COVERAGE:
        return None
    pairs_np = {b: (np.array(qs), np.array(rs)) for b, (qs, rs) in pairs.items()}
    # the canonical rig at rest: each bone's place under its parent
    bind_worlds = fk_worlds(canonical, canonical.frames[0])
    c_bind = {n.name: _matrix(bind_worlds[n.index]) for n in canonical.nodes}
    rest_local = {b: (_rigid_inverse(c_bind[p]) @ c_bind[b]) if p in c_parent else c_bind[b]
                  for b, p in c_parent.items()}
    report = Redraw()
    scales: list[float] = []
    for anim in anims.values():
        index_of = {n.name: n.index for n in anim.nodes}
        if any(b not in index_of for b in c_order):
            continue
        anim_names = {n.index: n.name for n in anim.nodes}
        root_parent = {n.name: anim_names.get(n.parent) for n in anim.nodes}
        frames: list[Frame] = []
        for frame in anim.frames:
            worlds = fk_worlds(anim, frame)
            their = {b: _matrix(worlds[index_of[b]]) for b in c_order}
            rotation: dict[str, np.ndarray] = {}
            place: dict[str, np.ndarray] = {}
            targets, posed_from = [], []
            for b in c_order:
                parent = c_parent[b]
                if b in pairs_np and len(pairs_np[b][0]) >= MIN_PAIRS:
                    qs, rs = pairs_np[b]
                    target = (their[b][:3, :3] @ rs.T).T + their[b][:3, 3]
                    rotation[b] = _kabsch(qs, target)
                    targets.append(target)
                    posed_from.append((b, qs))
                    spread_t = np.sqrt(((target - target.mean(0)) ** 2).sum(1).mean())
                    spread_q = np.sqrt(((qs - qs.mean(0)) ** 2).sum(1).mean())
                    if spread_q > 1e-6:
                        scales.append(spread_t / spread_q)
                elif parent in c_parent:
                    rotation[b] = rotation[parent] @ rest_local[b][:3, :3]
                else:
                    rotation[b] = their[b][:3, :3]
                place[b] = (rotation[parent] @ rest_local[b][:3, 3] + place[parent]
                            if parent in c_parent else np.zeros(3))
            if targets:
                posed = np.concatenate([(rotation[b] @ qs.T).T + place[b] for b, qs in posed_from])
                shift = np.concatenate(targets).mean(0) - posed.mean(0)
                for b in c_order:
                    place[b] = place[b] + shift
            world = {}
            for b in c_order:
                m = np.eye(4)
                m[:3, :3], m[:3, 3] = rotation[b], place[b]
                world[b] = m
            new_poses: dict[int, BonePose] = {}
            for b in c_order:
                parent = c_parent[b]
                if parent in c_parent:
                    base = world[parent]
                else:  # hung from the zombie's own bone, as their frog was
                    holder = root_parent.get(b)
                    base = (_matrix(worlds[index_of[holder]]) if holder is not None
                            else np.eye(4))
                local = _rigid_inverse(base) @ world[b]
                rot = tuple(tuple(float(x) for x in row) for row in local[:3, :3])
                new_poses[index_of[b]] = BonePose(
                    index_of[b], Vector3(*(float(x) for x in local[:3, 3])),
                    matrix_to_euler(rot))
            frames.append(Frame(frame.time, tuple(new_poses.get(p.bone, p)
                                                  for p in frame.poses)))
            report.frames += 1
        anim.frames = frames
        report.sequences += 1
    if scales:
        report.scale = float(np.median(scales))
    return report


__all__ = ["Redraw", "can_redraw", "redraw_grenade"]
