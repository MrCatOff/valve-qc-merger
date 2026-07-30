"""Per-frame animation de-clipping for the weight-transfer hands.

When the reference-hand mesh is weight-transferred onto the weapon's own bones,
the weapon's existing animations drive it for free -- but on the fastest poses
(draw, reload, off-hand shots) the fuller mesh pokes through the gun even though
the resting grip is clean.

This adjusts only the offending frames: for each finger that clips, it rotates
the finger's proximal joint (which swings the whole finger rigidly, mesh and all)
by the smallest rotation that lifts the mesh out of the gun. The penetration is
measured per finger with the same triangle test the collision detector uses, so
the search is verified every step. Frames and fingers that do not clip are left
untouched, so the resting grip is preserved exactly.
"""

from __future__ import annotations

from valve_qc_merger.collision import _bbox, _cells, _triangles_intersect
from valve_qc_merger.correspondence import HandLink
from valve_qc_merger.kinematics import world_transforms
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd
from valve_qc_merger.transform import Transform, axis_angle, mat3_multiply

_Vec = tuple[float, float, float]
_Tri = tuple[_Vec, _Vec, _Vec]
_STEP = 0.07  # rotation step per descent probe, radians (~4 deg)
_MAX_ANGLE = 0.6  # cap total proximal rotation, radians (~34 deg)
_ZERO = Vector3(0.0, 0.0, 0.0)
_AXES = (Vector3(1.0, 0.0, 0.0), Vector3(0.0, 1.0, 0.0), Vector3(0.0, 0.0, 1.0))


def _finger_chains(links: list[HandLink]) -> list[tuple[int, ...]]:
    """Weapon finger bone chains (proximal..tip), from the correspondence."""
    return [source.joints for link in links for source, _ in link.finger_pairs]


def _finger_mesh(hand: Smd, chain: tuple[int, ...]) -> list[tuple[tuple[int, Vector3], ...]]:
    """The hand triangles skinned to this finger, as (bone, local position) triples."""
    members = set(chain)
    out: list[tuple[tuple[int, Vector3], ...]] = []
    for triangle in hand.triangles:
        if any(v.bone in members for v in triangle.vertices):
            out.append(tuple((v.bone, v.position) for v in triangle.vertices))
    return out


def _pose_finger(
    base: dict[int, Transform], chain: tuple[int, ...], rot: Transform
) -> dict[int, Transform]:
    """``base`` world transforms with the chain re-posed by rotating its proximal joint."""
    proximal = base[chain[0]]
    spun = Transform(mat3_multiply(rot.rotation, proximal.rotation), proximal.translation)
    worlds = dict(base)
    worlds[chain[0]] = spun
    for k in range(1, len(chain)):
        local = base[chain[k - 1]].inverse().compose(base[chain[k]])
        worlds[chain[k]] = worlds[chain[k - 1]].compose(local)
    return worlds


def _finger_overlap(
    mesh: list[tuple[tuple[int, Vector3], ...]],
    worlds: dict[int, Transform],
    gun_tris: list[_Tri],
    gun_grid: dict[tuple[int, int, int], list[int]],
) -> int:
    """How many of this finger's triangles cross the gun (either direction)."""
    count = 0
    for tri in mesh:
        w = [worlds[bone].transform_point(pos) for bone, pos in tri]
        pts: _Tri = ((w[0].x, w[0].y, w[0].z), (w[1].x, w[1].y, w[1].z), (w[2].x, w[2].y, w[2].z))
        lo, hi = _bbox(pts)
        candidates: set[int] = set()
        for cell in _cells(lo, hi):
            candidates.update(gun_grid.get(cell, ()))
        if any(_triangles_intersect(pts, gun_tris[i]) for i in candidates):
            count += 1
    return count


def _gun_world(gun: Smd, world: dict[int, Transform]) -> list[_Tri]:
    tris: list[_Tri] = []
    for triangle in gun.triangles:
        pts = []
        for v in triangle.vertices:
            t = world.get(v.bone)
            if t is None:
                break
            p = t.transform_point(v.position)
            pts.append((p.x, p.y, p.z))
        if len(pts) == 3:
            tris.append((pts[0], pts[1], pts[2]))
    return tris


def declip_animation(
    animation: Smd,
    weapon_nodes: list[Node],
    hand: Smd,
    gun: Smd,
    links: list[HandLink],
    *,
    tolerance: int = 2,
) -> tuple[Smd, int]:
    """Return ``animation`` with clipping finger frames corrected, and a count.

    Only the proximal joint of a clipping finger is rotated, by the smallest
    rotation that drops its clipping-triangle count to ``tolerance`` or below;
    every other pose is preserved exactly, so the resting grip is untouched.
    """
    chains = _finger_chains(links)
    meshes = [_finger_mesh(hand, chain) for chain in chains]
    parent = {node.index: node.parent for node in weapon_nodes}

    corrected = 0
    new_frames: list[Frame] = []
    for frame in animation.frames:
        world = world_transforms(animation.nodes, frame)
        gun_tris = _gun_world(gun, world)
        grid: dict[tuple[int, int, int], list[int]] = {}
        for i, tri in enumerate(gun_tris):
            lo, hi = _bbox(tri)
            for cell in _cells(lo, hi):
                grid.setdefault(cell, []).append(i)

        poses = {p.bone: p for p in frame.poses}
        for chain, mesh in zip(chains, meshes, strict=True):
            rot = Transform.identity()
            overlap = _finger_overlap(mesh, world, gun_tris, grid)
            if overlap <= tolerance:
                continue
            angle = 0.0
            while overlap > tolerance and angle < _MAX_ANGLE:
                best = (overlap, rot)
                for ax in _AXES:
                    for sign in (_STEP, -_STEP):
                        trial = Transform(
                            mat3_multiply(axis_angle(ax, sign), rot.rotation), _ZERO
                        )
                        worlds = _pose_finger(world, chain, trial)
                        value = _finger_overlap(mesh, worlds, gun_tris, grid)
                        if value < best[0]:
                            best = (value, trial)
                if best[1] is rot:
                    break  # no probe improved -- local minimum
                overlap, rot = best
                angle += _STEP
            if angle == 0.0:
                continue
            # bake the proximal rotation into this frame's pose
            new_world_p = Transform(
                mat3_multiply(rot.rotation, world[chain[0]].rotation), world[chain[0]].translation
            )
            par = parent.get(chain[0], -1)
            parent_world = world.get(par, Transform.identity())
            local_p = parent_world.inverse().compose(new_world_p)
            old = poses[chain[0]]
            poses[chain[0]] = BonePose(chain[0], old.position, local_p.to_euler())
            corrected += 1
        new_frames.append(Frame(frame.time, tuple(sorted(poses.values(), key=lambda p: p.bone))))

    return Smd(version=animation.version, nodes=animation.nodes, frames=new_frames), corrected


__all__ = ["declip_animation"]
