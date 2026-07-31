"""Phase 6 — post-export verification on the emitted SMD text (§7.8), pure Python.

Parses the written SMDs and proves the §2 constraints at format level, catching
anything that leaked through the Blender layer. Every check is a plain data
comparison over :mod:`valve_qc_merger.parsers.smd` structures — no ``bpy``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.models.smd import Frame, Node, Smd
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.transform import (
    Matrix3,
    Transform,
    euler_to_matrix,
    mat3_multiply,
    mat3_transpose,
    rotation_angle,
)


@dataclass
class VerifyResult:
    """Outcome of the Phase 6 gate for one exported model."""

    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)

    def fail(self, check: str, message: str) -> None:
        self.ok = False
        self.checks[check] = False
        self.errors.append(f"[{check}] {message}")

    def passed(self, check: str) -> None:
        self.checks.setdefault(check, True)


def _node_table(smd: Smd) -> list[tuple[int, str, int]]:
    return [(n.index, n.name, n.parent) for n in smd.nodes]


def _is_finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


def verify_export(
    mesh_smds: Path | dict[str, Path],
    anim_smds: dict[str, Path],
    reference_smd: Path,
    *,
    hand_bones: set[str],
    anchor_bones: set[str],
    mesh_sources: dict[str, Path] | None = None,
    source_anims: dict[str, Path] | None = None,
    gun_bones: set[str] | None = None,
    weapon_offset: tuple[float, float, float] | None = None,
    epsilon: float = 1e-4,
    euler_jump_threshold_degrees: float = 120.0,
    geom_tolerance: float = 1e-3,
    weapon_pos_tolerance: float = 0.05,
    weapon_rot_tolerance_degrees: float = 1.0,
) -> VerifyResult:
    """Run every §7.8 check over the emitted model.

    ``mesh_smds`` maps exported mesh name to path — one merged hands+weapon SMD,
    or (bodygroup mode) a weapon SMD plus one per hand variant; all must share
    one node table with every animation. ``mesh_sources`` maps an exported mesh
    name to the INPUT mesh whose vertex positions it must preserve (the hand
    variants; defaults to the reference for a single merged mesh). ``hand_bones``
    are the reference bone names whose local translation must be frozen at rest
    in every frame (§2.2/§2.3); ``anchor_bones`` are excused (they carry the
    retargeted wrist translation). ``source_anims`` maps sequence name to the
    *input* animation SMD so frame counts/time indices can be cross-checked and —
    with ``gun_bones`` — so every weapon bone's FK world pose can be proven equal
    to the source's per frame (§7.5 zero offset, §7.7).
    """
    result = VerifyResult()
    if isinstance(mesh_smds, Path):
        mesh_smds = {"mesh": mesh_smds}
        mesh_sources = mesh_sources or {"mesh": reference_smd}
    meshes = {name: parse_smd_file(path) for name, path in sorted(mesh_smds.items())}
    mesh = next(iter(meshes.values()))  # canonical skeleton carrier
    reference = parse_smd_file(reference_smd)
    mesh_table = _node_table(mesh)

    for name, other in meshes.items():
        if _node_table(other) != mesh_table:
            result.fail("node_tables_identical",
                        f"mesh SMD {name} node table differs from the others")
    _check_node_tables(result, mesh_table, anim_smds)
    _check_reference_bones_preserved(result, reference, mesh)
    _check_reference_rest_preserved(result, reference, mesh, epsilon)
    _check_reference_first(result, mesh.nodes, reference)

    threshold = math.radians(euler_jump_threshold_degrees)
    rot_tol = math.radians(weapon_rot_tolerance_degrees)
    for name, path in sorted(anim_smds.items()):
        anim = parse_smd_file(path)
        _check_no_nan(result, name, anim)
        _check_frozen_hand_translation(
            result, name, anim, mesh, hand_bones, anchor_bones, epsilon
        )
        _check_euler_continuity(result, name, anim, threshold)
        _check_euler_component_continuity(result, name, anim)
        _check_frame_indices(result, name, anim)
        if source_anims and name in source_anims:
            source = parse_smd_file(source_anims[name])
            _check_frame_count(result, name, anim, source)
            if gun_bones:
                _check_weapon_pose(
                    result, name, anim, source, gun_bones, weapon_pos_tolerance, rot_tol,
                    offset=weapon_offset,
                )

    for name, source_path in sorted((mesh_sources or {}).items()):
        if name in meshes:
            _check_reference_geometry_preserved(
                result, parse_smd_file(source_path), meshes[name], geom_tolerance,
                label=name,
            )
    return result


def _check_node_tables(
    result: VerifyResult, mesh_table: list[tuple[int, str, int]], anim_smds: dict[str, Path]
) -> None:
    ok = True
    for name, path in sorted(anim_smds.items()):
        table = _node_table(parse_smd_file(path))
        if table != mesh_table:
            result.fail("node_tables_identical",
                        f"anim {name} node table differs from mesh SMD")
            ok = False
    if ok:
        result.passed("node_tables_identical")


def _check_reference_bones_preserved(result: VerifyResult, reference: Smd, mesh: Smd) -> None:
    """Every reference bone keeps its name and parent-by-name in the unified table."""
    mesh_by_name = {n.name: n for n in mesh.nodes}
    mesh_name_of = {n.index: n.name for n in mesh.nodes}
    ref_name_of = {n.index: n.name for n in reference.nodes}
    ok = True
    for node in reference.nodes:
        unified = mesh_by_name.get(node.name)
        if unified is None:
            result.fail("reference_bones_preserved", f"reference bone {node.name!r} missing")
            ok = False
            continue
        ref_parent = ref_name_of.get(node.parent) if node.parent != -1 else None
        uni_parent = mesh_name_of.get(unified.parent) if unified.parent != -1 else None
        if ref_parent != uni_parent:
            result.fail("reference_bones_preserved",
                        f"{node.name!r} parent changed: {ref_parent!r} -> {uni_parent!r}")
            ok = False
    if ok:
        result.passed("reference_bones_preserved")


def _check_reference_rest_preserved(
    result: VerifyResult, reference: Smd, mesh: Smd, epsilon: float
) -> None:
    """Every reference bone's rest local transform survives into the unified mesh.

    ``hand_translation_frozen`` baselines against the exported mesh's *own* rest
    frame, so a uniformly drifted or scaled rest would self-consistently pass it.
    This check closes that hole by comparing the exported rest against the *input*
    reference SMD, by name: translations componentwise within ``epsilon`` (plus
    float32 round-trip headroom) and rotations geodesically (BST may re-name the
    same rotation with a gimbal-equivalent Euler).
    """
    if not reference.frames or not mesh.frames:
        result.fail("reference_rest_preserved", "missing rest skeleton frame")
        return
    # Compare in WORLD space: Blender's first edit-mode roundtrip re-derives each
    # bone's local from head/tail/roll, which can rename a parent's roll by ~2e-4
    # rad and shift a child's *local* translation by lever-arm times that angle
    # while every bone stays exactly where it was. §2.1 is about where the bones
    # are; the local re-decomposition is reported as a warning, not a failure.
    ref_world = _fk_worlds(reference, reference.frames[0])
    mesh_world = _fk_worlds(mesh, mesh.frames[0])
    ref_by_name = {n.name: n.index for n in reference.nodes}
    mesh_by_name = {n.name: n.index for n in mesh.nodes}
    pos_tol = max(epsilon, 1e-3)  # float32 head positions at ~20u, FK-accumulated
    rot_tol = 1e-3  # radians, geodesic
    max_local = 0.0
    for name, ref_idx in ref_by_name.items():
        mesh_idx = mesh_by_name.get(name)
        if mesh_idx is None:
            continue  # reference_bones_preserved already reports missing bones
        rw, mw = ref_world[ref_idx], mesh_world[mesh_idx]
        dp = math.dist(
            (rw.translation.x, rw.translation.y, rw.translation.z),
            (mw.translation.x, mw.translation.y, mw.translation.z),
        )
        dr = rotation_angle(mat3_multiply(mw.rotation, mat3_transpose(rw.rotation)))
        if dp > pos_tol or dr > rot_tol:
            result.fail(
                "reference_rest_preserved",
                f"world rest of {name!r} drifted from the input reference: "
                f"pos {dp:.5f}u, rot {math.degrees(dr):.3f} deg (§2.1)",
            )
            return
        rp = next(p for p in reference.frames[0].poses if p.bone == ref_idx)
        mp = next(p for p in mesh.frames[0].poses if p.bone == mesh_idx)
        max_local = max(
            max_local,
            abs(mp.position.x - rp.position.x),
            abs(mp.position.y - rp.position.y),
            abs(mp.position.z - rp.position.z),
        )
    if max_local > 1e-4:
        result.warnings.append(
            f"[reference_rest_preserved] local rest decompositions re-derived by the "
            f"exporter (max component shift {max_local:.5f}u); world rest is unchanged"
        )
    result.passed("reference_rest_preserved")


def _check_reference_first(result: VerifyResult, nodes: list[Node], reference: Smd) -> None:
    """Reference bones should occupy the first N indices (§7.7). Non-fatal: BST
    hierarchy-sorts, so a gun bone parented under a wrist nests mid-table."""
    n_ref = len(reference.nodes)
    ref_names = {n.name for n in reference.nodes}
    leading = {n.name for n in nodes[:n_ref]}
    if leading != ref_names:
        strays = sorted(leading - ref_names)
        result.warnings.append(
            f"[reference_first] reference bones are not the first {n_ref} nodes "
            f"(interleaved: {strays}); node tables are still identical across SMDs"
        )


def _rest_local(mesh: Smd) -> dict[int, tuple[float, float, float]]:
    """Bind (rest) local translation per bone index, from the mesh SMD's one frame."""
    if not mesh.frames:
        return {}
    return {p.bone: (p.position.x, p.position.y, p.position.z) for p in mesh.frames[0].poses}


def _check_no_nan(result: VerifyResult, name: str, anim: Smd) -> None:
    for frame in anim.frames:
        for pose in frame.poses:
            if not _is_finite(pose.position.x, pose.position.y, pose.position.z,
                              pose.rotation.x, pose.rotation.y, pose.rotation.z):
                result.fail("no_nan_inf", f"{name} frame {frame.time} bone {pose.bone} non-finite")
                return
    result.passed("no_nan_inf")


def _check_frozen_hand_translation(
    result: VerifyResult, name: str, anim: Smd, mesh: Smd,
    hand_bones: set[str], anchor_bones: set[str], epsilon: float,
) -> None:
    """Hand bones (bar anchors) keep their rest local translation every frame."""
    name_of = {n.index: n.name for n in mesh.nodes}
    rest = _rest_local(mesh)
    watch = {i for i, nm in name_of.items()
             if nm in hand_bones and nm not in anchor_bones}
    for frame in anim.frames:
        for pose in frame.poses:
            if pose.bone not in watch or pose.bone not in rest:
                continue
            rx, ry, rz = rest[pose.bone]
            if (abs(pose.position.x - rx) > epsilon
                    or abs(pose.position.y - ry) > epsilon
                    or abs(pose.position.z - rz) > epsilon):
                result.fail(
                    "hand_translation_frozen",
                    f"{name} frame {frame.time} bone {name_of[pose.bone]!r} translation "
                    f"drifted from rest by > {epsilon}",
                )
                return
    result.passed("hand_translation_frozen")


def _check_euler_continuity(
    result: VerifyResult, name: str, anim: Smd, threshold: float
) -> None:
    """No bone's *rotation* teleports between adjacent frames (§7.8).

    The check is rotation-aware. The emitted tracks are Euler-unwrapped upstream
    (:mod:`euler_unwrap`), so a large residual component jump is real motion, not a
    naming flip; the invariant that actually matters for playback is that the
    geodesic rotation between consecutive frames stays within the threshold. That
    catches a genuine pop/teleport while accepting fast-but-smooth motion (e.g. a
    finger snapping during a 30 fps reload).
    """
    prev: dict[int, Matrix3] = {}
    for frame in anim.frames:
        for pose in frame.poses:
            cur = euler_to_matrix(pose.rotation)
            last = prev.get(pose.bone)
            if last is not None:
                delta = rotation_angle(mat3_multiply(cur, mat3_transpose(last)))
                if delta > threshold:
                    result.fail(
                        "euler_continuity",
                        f"{name} bone {pose.bone} rotation jumps "
                        f"{math.degrees(delta):.0f} deg (> {math.degrees(threshold):.0f}) "
                        f"at frame {frame.time}",
                    )
                    return
            prev[pose.bone] = cur
    result.passed("euler_continuity")


def _check_euler_component_continuity(result: VerifyResult, name: str, anim: Smd) -> None:
    """No Euler *component* jumps by more than pi between adjacent frames (§7.8).

    After a correct unwrap every component is wrapped within pi of the previous
    frame, so a larger jump can only mean the unwrap was skipped or broken — the
    naming discontinuity that GoldSrc's per-component interpolation turns into a
    visible spin. This is the per-component check the spec asks for; the geodesic
    check above guards real motion, this one guards the representation.
    """
    limit = math.pi + 1e-6
    prev: dict[int, tuple[float, float, float]] = {}
    for frame in anim.frames:
        for pose in frame.poses:
            cur = (pose.rotation.x, pose.rotation.y, pose.rotation.z)
            last = prev.get(pose.bone)
            if last is not None:
                for axis, (a, b) in zip("xyz", zip(last, cur, strict=True), strict=True):
                    if abs(b - a) > limit:
                        result.fail(
                            "euler_component_continuity",
                            f"{name} bone {pose.bone} euler {axis} jumps "
                            f"{math.degrees(abs(b - a)):.0f} deg at frame {frame.time} "
                            "(unwrap missing or broken)",
                        )
                        return
            prev[pose.bone] = cur
    result.passed("euler_component_continuity")


def _fk_worlds(smd: Smd, frame: Frame) -> dict[int, Transform]:
    """World transform per bone index for one skeleton frame, by FK over the nodes."""
    local = {
        p.bone: Transform.from_pos_euler(p.position, p.rotation) for p in frame.poses
    }
    parent_of = {n.index: n.parent for n in smd.nodes}
    world: dict[int, Transform] = {}

    def resolve(index: int) -> Transform:
        cached = world.get(index)
        if cached is not None:
            return cached
        parent = parent_of[index]
        xf = local[index] if parent < 0 else resolve(parent).compose(local[index])
        world[index] = xf
        return xf

    for n in smd.nodes:
        resolve(n.index)
    return world


def _check_weapon_pose(
    result: VerifyResult, name: str, anim: Smd, source: Smd,
    gun_bones: set[str], pos_tolerance: float, rot_tolerance: float,
    offset: tuple[float, float, float] | None = None,
) -> None:
    """Every weapon bone's FK world pose equals the source's, per frame (§7.5/§7.7).

    The placement guarantee — the weapon stays exactly where its own animation
    puts it, rigidly translated by the configured constant ``offset`` if any —
    proven from the emitted text. This is the check that catches a broken
    animation transfer (e.g. rotation keys that never took), which the hand-centric
    checks are structurally blind to.
    """
    ox, oy, oz = offset or (0.0, 0.0, 0.0)
    anim_idx = {n.name: n.index for n in anim.nodes if n.name in gun_bones}
    src_idx = {n.name: n.index for n in source.nodes if n.name in gun_bones}
    shared = sorted(anim_idx.keys() & src_idx.keys())
    if not shared:
        result.fail("weapon_pose_matches_source", f"{name}: no gun bones found to compare")
        return
    # frame_count reports length mismatches; compare the overlapping prefix here.
    for exported_frame, source_frame in zip(anim.frames, source.frames, strict=False):
        worlds = _fk_worlds(anim, exported_frame)
        src_worlds = _fk_worlds(source, source_frame)
        for bone in shared:
            ours = worlds[anim_idx[bone]]
            src = src_worlds[src_idx[bone]]
            dp = math.dist(
                (ours.translation.x, ours.translation.y, ours.translation.z),
                (src.translation.x + ox, src.translation.y + oy, src.translation.z + oz),
            )
            dr = rotation_angle(mat3_multiply(ours.rotation, mat3_transpose(src.rotation)))
            if dp > pos_tolerance or dr > rot_tolerance:
                result.fail(
                    "weapon_pose_matches_source",
                    f"{name} frame {exported_frame.time} bone {bone!r} deviates from the "
                    f"source weapon pose: pos {dp:.3f}u, rot {math.degrees(dr):.1f} deg "
                    f"(tol {pos_tolerance}u / {math.degrees(rot_tolerance):.1f} deg)",
                )
                return
    result.passed("weapon_pose_matches_source")


def _check_frame_indices(result: VerifyResult, name: str, anim: Smd) -> None:
    times = [f.time for f in anim.frames]
    if not times:
        result.fail("frame_indices", f"{name} has no frames")
        return
    if times[0] != 0:
        result.fail("frame_indices", f"{name} first frame time is {times[0]}, expected 0")
        return
    if times != list(range(times[0], times[0] + len(times))):
        result.fail("frame_indices", f"{name} frame times are not contiguous: {times}")
        return
    result.passed("frame_indices")


def _check_frame_count(result: VerifyResult, name: str, anim: Smd, source: Smd) -> None:
    if anim.frame_count != source.frame_count:
        result.fail(
            "frame_count",
            f"{name} has {anim.frame_count} frames, source has {source.frame_count}",
        )
        return
    result.passed("frame_count")


def _check_reference_geometry_preserved(
    result: VerifyResult, reference: Smd, mesh: Smd, geom_tolerance: float,
    label: str = "mesh",
) -> None:
    """Every reference hand vertex survives into the merged mesh SMD (§2.1).

    Byte-identity is impossible through Blender — BST recomputes vertex normals and
    may reorder/re-weld triangles on export. The reshape-relevant invariant is that
    no hand vertex *moved*: for every reference vertex position there must be a mesh
    vertex within ``geom_tolerance`` model units. That directly proves the hands
    were not scaled or reshaped, independent of normal round-tripping.
    """
    ref_pts = {(v.position.x, v.position.y, v.position.z)
               for tri in reference.triangles for v in tri.vertices}
    mesh_pts = {(v.position.x, v.position.y, v.position.z)
                for tri in mesh.triangles for v in tri.vertices}
    grid = _spatial_grid(mesh_pts, geom_tolerance)
    missing = 0
    max_dev = 0.0
    for pt in ref_pts:
        dev = _nearest(pt, grid, geom_tolerance)
        if dev is None:
            missing += 1
        else:
            max_dev = max(max_dev, dev)
    if missing:
        result.fail(
            "reference_mesh_preserved",
            f"{label}: {missing}/{len(ref_pts)} hand vertices moved by more than "
            f"{geom_tolerance} model units (hand reshaped)",
        )
        return
    if max_dev > 0:
        result.warnings.append(
            f"[reference_mesh_preserved] {label}: hand vertices preserved within "
            f"{max_dev:.5f} model units (normals recomputed by the exporter)"
        )
    result.passed("reference_mesh_preserved")


def _spatial_grid(
    points: set[tuple[float, float, float]], cell: float
) -> dict[tuple[int, int, int], list[tuple[float, float, float]]]:
    grid: dict[tuple[int, int, int], list[tuple[float, float, float]]] = {}
    for p in points:
        key = (int(p[0] // cell), int(p[1] // cell), int(p[2] // cell))
        grid.setdefault(key, []).append(p)
    return grid


def _nearest(
    pt: tuple[float, float, float],
    grid: dict[tuple[int, int, int], list[tuple[float, float, float]]],
    tol: float,
) -> float | None:
    """Distance to the nearest grid point within ``tol``, or None if none is close."""
    cx, cy, cz = int(pt[0] // tol), int(pt[1] // tol), int(pt[2] // tol)
    best: float | None = None
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                for q in grid.get((cx + dx, cy + dy, cz + dz), ()):
                    d = math.dist(pt, q)
                    if d <= tol and (best is None or d < best):
                        best = d
    return best


__all__ = ["VerifyResult", "verify_export"]
