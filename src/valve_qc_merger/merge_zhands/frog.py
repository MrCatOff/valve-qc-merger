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

import re
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


def _frog_vertices(smd: Smd, prefix: str) -> dict[tuple, list[tuple[str, np.ndarray, np.ndarray]]]:
    """UV (+ bone) -> every distinct vertex there: (bone, its place in the
    bone's bind frame, its place in the grenade root's bind frame). A frog's
    left and right halves share their UVs (the eyes, the sides of the head),
    so one key can hold several vertices."""
    worlds = fk_worlds(smd, smd.frames[0])
    inverse = {i: _rigid_inverse(_matrix(xf)) for i, xf in worlds.items()}
    name_of = {n.index: n.name for n in smd.nodes}
    parent_of = {n.index: n.parent for n in smd.nodes}
    gren = {i for i, name in name_of.items() if name.startswith(prefix)}
    roots = sorted((i for i in gren if parent_of[i] not in gren), key=lambda i: name_of[i])
    to_root = inverse[roots[0]] if roots else np.eye(4)
    out: dict[tuple, list[tuple[str, np.ndarray, np.ndarray]]] = {}
    seen: set[tuple] = set()
    for tri in smd.triangles:
        for v in tri.vertices:
            if v.bone not in gren:
                continue
            bone = name_of[v.bone]
            point = np.array([v.position.x, v.position.y, v.position.z, 1.0])
            key = (round(v.uv.u, 4), round(v.uv.v, 4), bone)
            ident = (*key, round(float(point[0]), 3), round(float(point[1]), 3),
                     round(float(point[2]), 3))
            if ident in seen:  # the same vertex again (shared corners, two-sided)
                continue
            seen.add(ident)
            out.setdefault(key, []).append(
                (bone, (inverse[v.bone] @ point)[:3], (to_root @ point)[:3]))
    return out


def _similarity(source: np.ndarray, target: np.ndarray, reflect: bool = False
                ) -> tuple[np.ndarray, float, np.ndarray, float]:
    """Linear part (a rotation; a reflection too with ``reflect``), scale,
    translation laying ``source`` onto ``target`` (Umeyama), and the mean
    squared residual."""
    sc, tc = source.mean(0), target.mean(0)
    h = (source - sc).T @ (target - tc)
    u, sv, vt = np.linalg.svd(h)
    d = 1.0 if reflect else (np.sign(np.linalg.det(vt.T @ u.T)) or 1.0)
    if reflect and np.linalg.det(vt.T @ u.T) > 0:
        d = -1.0
    linear = vt.T @ np.diag([1.0, 1.0, d]) @ u.T
    var = ((source - sc) ** 2).sum()
    scale = float((sv * np.array([1.0, 1.0, d])).sum() / var) if var > 1e-12 else 1.0
    shift = tc - scale * linear @ sc
    residual = float(np.mean(np.sum((source @ (scale * linear).T + shift - target) ** 2, 1)))
    return linear, scale, shift, residual


Pairs = dict[str, tuple[np.ndarray, list[str], np.ndarray]]


def _pair(canonical: Smd, theirs: Smd, prefix: str) -> Pairs | None:
    """canonical bone -> (its bone-local points, the bone each one's
    counterpart rides, the counterparts' bone-local points): first by the
    UVs only one vertex has, then — the two frogs laid on each other by
    those — every vertex to its nearest counterpart of the same UV (the left
    and right halves share UVs: pairing the left eye with the right one
    mirrored the frog). A frog that is a mirror image of ours (v_china) is
    paired across: our left half with its right. None when too few pair up."""
    c_verts, t_verts = _frog_vertices(canonical, prefix), _frog_vertices(theirs, prefix)
    total = sum(len(v) for v in c_verts.values())
    if total == 0:
        return None
    unique = [(c_verts[k][0], t_verts[k][0]) for k in c_verts
              if k in t_verts and len(c_verts[k]) == 1 and len(t_verts[k]) == 1]
    if len(unique) < 4:
        return None
    source = np.array([c[2] for c, _t in unique])
    target = np.array([t[2] for _c, t in unique])
    linear, scale, shift, residual = _similarity(source, target)
    mirror = _similarity(source, target, reflect=True)
    mirrored = mirror[3] < 0.25 * residual  # their frog is ours in a mirror
    if mirrored:
        linear, scale, shift, residual = mirror
    # every candidate of their frog, laid where ours is
    candidates = [(bone, r, root) for verts in t_verts.values() for bone, r, root in verts]
    t_roots = np.array([root for _b, _r, root in candidates])
    index_of_key: dict[tuple, list[int]] = {}
    position = 0
    for key, verts in t_verts.items():
        index_of_key[key] = list(range(position, position + len(verts)))
        position += len(verts)
    pairs: dict[str, tuple[list[np.ndarray], list[str], list[np.ndarray]]] = {}
    paired = 0
    for key, mine in c_verts.items():
        for bone, q, root_point in mine:
            moved = scale * linear @ root_point + shift
            # the same UV on the same bone — on the other side's bone first
            # when their frog is mirrored (our left eye is its right eye)
            same = index_of_key.get(key, [])
            across = index_of_key.get((key[0], key[1], _other_side(key[2])), [])
            pool = (across or same) if mirrored else (same or across)
            if not pool:
                continue
            best = min(pool, key=lambda i: float(np.sum((t_roots[i] - moved) ** 2)))
            other_bone, r, _root = candidates[best]
            entry = pairs.setdefault(bone, ([], [], []))
            entry[0].append(q)
            entry[1].append(other_bone)
            entry[2].append(r)
            paired += 1
    if paired / total < MIN_COVERAGE:
        return None
    return {b: (np.array(qs), names, np.array(rs)) for b, (qs, names, rs) in pairs.items()}


def _other_side(bone: str) -> str:
    """The bone of the other half (EyeL <-> EyeR, _L_ <-> _R_)."""
    swapped = re.sub(r"(?<![A-Za-z])([LlRr])(?![a-z])", lambda m: {"L": "R", "R": "L",
                     "l": "r", "r": "l"}[m.group(1)], bone)
    if swapped != bone:
        return swapped
    for a, b in (("Left", "Right"), ("left", "right"), ("L", "R")):
        if bone.endswith(a):
            return bone[: -len(a)] + b
        if bone.endswith(b):
            return bone[: -len(b)] + a
    return bone


def _mirrored(pairs: Pairs) -> bool:
    """Is most of their frog (by points) a mirror image of ours, part by
    part? A rotation cannot draw that — the frog would come out flipped.
    (A lone small part can come out negative on nearly flat points.)"""
    flipped = kept = 0
    for qs, names, rs in pairs.values():
        if len(qs) < MIN_PAIRS or len(set(names)) != 1:
            continue  # (points of several bones: no one frame to compare in)
        h = (qs - qs.mean(0)).T @ (rs - rs.mean(0))
        u, sv, vt = np.linalg.svd(h)
        if sv[1] <= 1e-6:
            continue
        if np.linalg.det(vt.T @ u.T) < 0:
            flipped += len(qs)
        else:
            kept += len(qs)
    return flipped > kept


def can_redraw(canonical: Smd, theirs: Smd, prefix: str) -> bool:
    """Do the two frogs pair up (same grenade bones, most vertices by UV)?"""
    c_parent, _o = _gren(canonical, prefix)
    t_parent, _t = _gren(theirs, prefix)
    if set(c_parent) != set(t_parent):
        return False
    pairs = _pair(canonical, theirs, prefix)
    return pairs is not None and not _mirrored(pairs)


def mirror_frog(smd: Smd, prefix: str) -> Smd:
    """``smd``'s frog in a mirror (across its root's local YZ plane), winding
    turned so it still faces out: the shape a mirrored frog (v_china's) can
    be redrawn on with rotations alone — bones cannot mirror."""
    from valve_qc_merger.models.smd import Triangle, Vertex
    worlds = fk_worlds(smd, smd.frames[0])
    name_of = {n.index: n.name for n in smd.nodes}
    parent_of = {n.index: n.parent for n in smd.nodes}
    gren = {i for i, name in name_of.items() if name.startswith(prefix)}
    roots = sorted((i for i in gren if parent_of[i] not in gren), key=lambda i: name_of[i])
    root = _matrix(worlds[roots[0]]) if roots else np.eye(4)
    flip = root @ np.diag([-1.0, 1.0, 1.0, 1.0]) @ _rigid_inverse(root)

    def point(v: Vector3) -> Vector3:
        return Vector3(*(float(x) for x in (flip @ np.array([v.x, v.y, v.z, 1.0]))[:3]))

    def normal(v: Vector3) -> Vector3:
        return Vector3(*(float(x) for x in flip[:3, :3] @ np.array([v.x, v.y, v.z])))

    triangles = []
    for tri in smd.triangles:
        a, b, c = (Vertex(v.bone, point(v.position), normal(v.normal), v.uv)
                   for v in tri.vertices)
        triangles.append(Triangle(tri.material, (a, c, b)))
    return Smd(nodes=list(smd.nodes), frames=list(smd.frames), triangles=triangles)


def redraw_grenade(canonical: Smd, theirs: Smd, anims: dict[str, Smd],
                   prefix: str) -> Redraw | None:
    """Rewrite the grenade bones of ``anims`` (one zombie's sequences, grenade
    bones named ``prefix…``) so ``canonical``'s frog follows ``theirs``.
    None (nothing changed) when the two frogs cannot be paired."""
    c_parent, c_order = _gren(canonical, prefix)
    t_parent, _t_order = _gren(theirs, prefix)
    if set(c_parent) != set(t_parent):
        return None
    pairs_np = _pair(canonical, theirs, prefix)
    if pairs_np is None or _mirrored(pairs_np):
        return None
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
                    qs, names, rs = pairs_np[b]
                    target = np.array([their[n][:3, :3] @ r + their[n][:3, 3]
                                       for n, r in zip(names, rs, strict=True)])
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


__all__ = ["Redraw", "can_redraw", "mirror_frog", "redraw_grenade"]
