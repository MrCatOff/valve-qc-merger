"""Finger-curl grip solve (Phase 4, §7.6), hinge-constrained.

Pure Python (no ``bpy``): curl each reference finger onto the weapon so its tip
reaches where the *original* finger tip rested (plus any configured weapon
offset), in the already-retargeted pose. The reference fingers are longer than
the original's, so targeting the original tip makes the extra length wrap
further around the grip instead of poking through it.

Anatomy is enforced structurally, not by per-axis Euler clamps: every joint of a
finger rotates about ONE fixed world hinge axis (the hand's knuckle axis for the
four fingers; the base-to-target arc plane normal for the thumb), with scalar
flexion limits per joint depth. A free 3-DOF CCD swing clamped at generous
per-axis bounds can and did fold joints sideways and backward into anatomically
impossible shapes; a hinge cannot — the curl is planar by construction.

The solver is Cyclic Coordinate Descent over the chain's hinge angles in each
joint's local ``matrix_basis`` (so it composes with Phase 2b's output):

    world(bone) = world(parent) . rest_local(bone) . basis(bone)

Warm-starting from the previous frame's angles keeps the motion temporally
coherent and cheap.
"""

from __future__ import annotations

import math

from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.transform import Matrix3, Transform, axis_angle, mat3_transpose


def rest_locals(chain: list[str], parent_of: dict[str, str | None],
                tgt_rest: dict[str, Transform]) -> dict[str, Transform]:
    """Parent-relative rest transform for each bone in a finger chain."""
    out: dict[str, Transform] = {}
    for bone in chain:
        parent = parent_of[bone]
        parent_rest = tgt_rest[parent] if parent is not None else Transform.identity()
        out[bone] = parent_rest.inverse().compose(tgt_rest[bone])
    return out


def _rot(m: Matrix3, v: Vector3) -> Vector3:
    return Vector3(
        m[0][0] * v.x + m[0][1] * v.y + m[0][2] * v.z,
        m[1][0] * v.x + m[1][1] * v.y + m[1][2] * v.z,
        m[2][0] * v.x + m[2][1] * v.y + m[2][2] * v.z,
    )


def _fk(chain: list[str], base_parent: Transform, rest_local: dict[str, Transform],
        basis: dict[str, Transform]) -> dict[str, Transform]:
    posed: dict[str, Transform] = {}
    prev = base_parent
    for bone in chain:
        posed[bone] = prev.compose(rest_local[bone]).compose(basis[bone])
        prev = posed[bone]
    return posed


def _norm(v: Vector3) -> Vector3:
    length = v.length() or 1.0
    return Vector3(v.x / length, v.y / length, v.z / length)


def _project_off_axis(v: Vector3, axis: Vector3) -> Vector3:
    d = v.x * axis.x + v.y * axis.y + v.z * axis.z
    return Vector3(v.x - d * axis.x, v.y - d * axis.y, v.z - d * axis.z)


def _signed_angle(a: Vector3, b: Vector3, axis: Vector3) -> float:
    """Signed angle from ``a`` to ``b`` about ``axis`` (all in the same space)."""
    cx = a.y * b.z - a.z * b.y
    cy = a.z * b.x - a.x * b.z
    cz = a.x * b.y - a.y * b.x
    sin = cx * axis.x + cy * axis.y + cz * axis.z
    cos = a.x * b.x + a.y * b.y + a.z * b.z
    return math.atan2(sin, cos)


def _bases_from_angles(
    chain: list[str], dof: list[str], base_parent: Transform,
    rest_local: dict[str, Transform], axis: Vector3, angles: dict[str, float],
    pre_basis: dict[str, Transform] | None = None,
) -> dict[str, Transform]:
    """Basis transforms realising hinge ``angles`` about the world ``axis``.

    Each joint's local hinge axis is the world axis expressed in that joint's
    seat frame (parent pose . rest local . pre basis), so the hinge rides with
    the chain as proximal joints curl — the anatomical behaviour. ``pre_basis``
    is an optional per-bone rotation applied before the hinge (the abduction
    aim that matches the source's finger spacing); the hinge curls on top.
    """
    basis: dict[str, Transform] = {b: Transform.identity() for b in chain}
    prev = base_parent
    for bone in chain:
        pre = pre_basis.get(bone) if pre_basis else None
        seat = prev.compose(rest_local[bone])
        if pre is not None:
            seat = seat.compose(pre)
        theta = angles.get(bone, 0.0)
        hinge = Transform.identity()
        if bone in dof and theta:
            local_axis = _norm(_rot(mat3_transpose(seat.rotation), axis))
            hinge = Transform(axis_angle(local_axis, theta))
        basis[bone] = pre.compose(hinge) if pre is not None else hinge
        prev = prev.compose(rest_local[bone]).compose(basis[bone])
    return basis


def solve_finger(
    chain: list[str],
    dof: list[str],
    base_parent_world: Transform,
    rest_local: dict[str, Transform],
    target_tip: Vector3,
    limits: dict[str, tuple[float, float]],
    *,
    axis: Vector3,
    pre_basis: dict[str, Transform] | None = None,
    warm_start: dict[str, float] | None = None,
    max_step: float | None = None,
    iterations: int = 12,
    tolerance: float = 1e-3,
) -> tuple[dict[str, Transform], dict[str, float]]:
    """Hinge-CCD one finger chain toward ``target_tip`` about a fixed world axis.

    ``chain`` is base..tip (the tip bone, e.g. a held ``*Nub``, is not a DOF);
    ``dof`` is the curlable subset; ``limits`` maps each DOF bone to a scalar
    (lo, hi) hinge range in radians. Returns the basis for every chain bone plus
    the solved hinge angles (the warm start for the next frame).

    ``max_step`` is the §7.6 temporal regularisation in hinge form: with a warm
    start, no joint may move more than this many radians from its previous-frame
    angle — a solution that legitimately wants to jump (e.g. the target crossing
    the hinge line reverses its planar projection) is spread over several frames
    instead of popping, at the cost of a transiently larger tip error.
    """
    axis = _norm(axis)
    angles: dict[str, float] = {b: 0.0 for b in dof}
    if warm_start:
        for b in dof:
            if b in warm_start:
                lo, hi = limits.get(b, (-math.pi, math.pi))
                angles[b] = min(hi, max(lo, warm_start[b]))

    for _ in range(iterations):
        basis = _bases_from_angles(
            chain, dof, base_parent_world, rest_local, axis, angles, pre_basis
        )
        posed = _fk(chain, base_parent_world, rest_local, basis)
        if posed[chain[-1]].translation.distance_to(target_tip) < tolerance:
            break
        for joint in reversed(dof):
            basis = _bases_from_angles(
                chain, dof, base_parent_world, rest_local, axis, angles, pre_basis
            )
            posed = _fk(chain, base_parent_world, rest_local, basis)
            tip = posed[chain[-1]].translation
            pivot = posed[joint].translation
            to_tip = _project_off_axis(Vector3(
                tip.x - pivot.x, tip.y - pivot.y, tip.z - pivot.z), axis)
            to_target = _project_off_axis(Vector3(
                target_tip.x - pivot.x, target_tip.y - pivot.y, target_tip.z - pivot.z),
                axis)
            if to_tip.length() < 1e-6 or to_target.length() < 1e-6:
                continue
            delta = _signed_angle(_norm(to_tip), _norm(to_target), axis)
            lo, hi = limits.get(joint, (-math.pi, math.pi))
            angles[joint] = min(hi, max(lo, angles[joint] + delta))

    if warm_start is not None and max_step is not None:
        for b in dof:
            prev = warm_start.get(b)
            if prev is not None:
                angles[b] = min(prev + max_step, max(prev - max_step, angles[b]))

    basis = _bases_from_angles(
        chain, dof, base_parent_world, rest_local, axis, angles, pre_basis
    )
    return basis, angles


def solve_finger_joints(
    chain: list[str],
    dof: list[str],
    base_parent_world: Transform,
    rest_local: dict[str, Transform],
    targets: dict[str, Vector3],
    limits: dict[str, tuple[float, float]],
    *,
    axis: Vector3,
    pre_basis: dict[str, Transform] | None = None,
    warm_start: dict[str, float] | None = None,
    max_step: float | None = None,
) -> tuple[dict[str, Transform], dict[str, float]]:
    """Per-joint hinge solve: every DOF joint places its CHILD on target.

    Tip-only CCD is redundant — many (MCP, PIP) combinations reach one tip
    point, and warm-started CCD keeps whatever knuckle bend it inherited.
    Targeting each joint's child position (the ORIGINAL hand's joint
    positions — its contact line on the weapon) removes the redundancy: the
    proximal joint rotates so the middle joint lands on the original middle
    joint, the middle so the distal lands on the original distal, root to
    tip, each about the shared hinge axis (projected into the hinge plane and
    clamped to anatomy). This reproduces the authored big-hand articulation:
    flatter knuckle, deeper curl.
    """
    axis = _norm(axis)
    angles: dict[str, float] = {}
    for joint in dof:
        j = chain.index(joint)
        child = chain[j + 1] if j + 1 < len(chain) else None
        target = targets.get(child) if child is not None else None
        if child is None or target is None:
            continue
        basis = _bases_from_angles(
            chain, dof, base_parent_world, rest_local, axis, angles, pre_basis
        )
        posed = _fk(chain, base_parent_world, rest_local, basis)
        pivot = posed[joint].translation
        cur = posed[child].translation
        to_cur = _project_off_axis(Vector3(
            cur.x - pivot.x, cur.y - pivot.y, cur.z - pivot.z), axis)
        to_tgt = _project_off_axis(Vector3(
            target.x - pivot.x, target.y - pivot.y, target.z - pivot.z), axis)
        if to_cur.length() < 1e-6 or to_tgt.length() < 1e-6:
            continue
        delta = _signed_angle(_norm(to_cur), _norm(to_tgt), axis)
        lo, hi = limits.get(joint, (-math.pi, math.pi))
        angles[joint] = min(hi, max(lo, delta))

    if warm_start is not None and max_step is not None:
        for b in dof:
            prev = warm_start.get(b)
            if prev is not None and b in angles:
                angles[b] = min(prev + max_step, max(prev - max_step, angles[b]))

    basis = _bases_from_angles(
        chain, dof, base_parent_world, rest_local, axis, angles, pre_basis
    )
    return basis, angles


def solve_finger_wrap(
    chain: list[str],
    dof: list[str],
    base_parent_world: Transform,
    rest_local: dict[str, Transform],
    axis_point: Vector3,
    radii: dict[str, float],
    limits: dict[str, tuple[float, float]],
    *,
    axis: Vector3,
    point_targets: dict[str, Vector3] | None = None,
    pre_basis: dict[str, Transform] | None = None,
    warm_start: dict[str, float] | None = None,
    max_step: float | None = None,
) -> tuple[dict[str, Transform], dict[str, float]]:
    """Wrap a finger around the grip cylinder the ORIGINAL fingers define.

    The source hand's finger joints lie on the weapon's grip surface; the
    grip is a cylinder through ``axis_point`` along ``axis`` with each
    child's radius taken from the SOURCE joint. Every DOF joint curls (in
    the calibrated positive direction) until its child meets the cylinder:
    the child's hinge circle (centre = the joint, in the hinge plane) is
    intersected with the grip circle, and the FIRST intersection reached by
    curling is taken — never the uncurl direction, so longer reference
    segments travel further around the grip exactly like an authored
    big-hand grip (gold pair: flatter knuckle, deeper curl). A size-matched
    hand meets the cylinder immediately and keeps the source pose.
    """
    axis = _norm(axis)

    def plane(v: Vector3) -> tuple[float, float]:
        # 2-D coordinates in the hinge plane (any fixed orthonormal pair).
        return (v.x * _U.x + v.y * _U.y + v.z * _U.z,
                v.x * _W.x + v.y * _W.y + v.z * _W.z)

    # Fixed in-plane basis (u, w) with w = axis x u, so the signed rotation
    # from one in-plane direction to another equals the hinge angle delta.
    seed = Vector3(1.0, 0.0, 0.0)
    if abs(axis.x) > 0.9:
        seed = Vector3(0.0, 1.0, 0.0)
    _U = _norm(_project_off_axis(seed, axis))
    _W = Vector3(axis.y * _U.z - axis.z * _U.y,
                 axis.z * _U.x - axis.x * _U.z,
                 axis.x * _U.y - axis.y * _U.x)

    a2 = plane(axis_point)
    angles: dict[str, float] = {b: 0.0 for b in dof}
    for _pass in range(3):
        for joint in dof:
            j = chain.index(joint)
            child = chain[j + 1] if j + 1 < len(chain) else None
            radius = radii.get(child) if child is not None else None
            if child is None or radius is None:
                continue
            basis = _bases_from_angles(
                chain, dof, base_parent_world, rest_local, axis, angles, pre_basis
            )
            posed = _fk(chain, base_parent_world, rest_local, basis)
            p2 = plane(posed[joint].translation)
            c2 = plane(posed[child].translation)
            cx, cy = c2[0] - p2[0], c2[1] - p2[1]
            big_l = math.hypot(cx, cy)
            point = (point_targets or {}).get(child)
            if point is not None:
                # Knuckle-line joint: aim the child at the ORIGINAL joint's
                # position (the authored knuckle stays OFF the grip surface).
                t2 = plane(point)
                tx, ty = t2[0] - p2[0], t2[1] - p2[1]
                if big_l < 1e-6 or math.hypot(tx, ty) < 1e-6:
                    continue
                delta = math.atan2(cx * ty - cy * tx, cx * tx + cy * ty)
                lo, hi = limits.get(joint, (-math.pi, math.pi))
                angles[joint] = min(hi, max(lo, angles[joint] + delta))
                continue
            dx, dy = a2[0] - p2[0], a2[1] - p2[1]
            d = math.hypot(dx, dy)
            if big_l < 1e-6 or d < 1e-6:
                continue
            # Intersect the child's hinge circle (P, L) with the grip circle
            # (A, r); no intersection => the child cannot reach the surface,
            # leave the direction-transferred pose alone.
            if d > big_l + radius or d < abs(big_l - radius):
                continue
            h = (big_l * big_l - radius * radius + d * d) / (2.0 * d)
            q = max(0.0, big_l * big_l - h * h) ** 0.5
            mx, my = p2[0] + h * dx / d, p2[1] + h * dy / d
            candidates = [
                (mx - q * dy / d, my + q * dx / d),
                (mx + q * dy / d, my - q * dx / d),
            ]
            deltas = []
            for tx, ty in candidates:
                delta = math.atan2(cx * (ty - p2[1]) - cy * (tx - p2[0]),
                                   cx * (tx - p2[0]) + cy * (ty - p2[1]))
                deltas.append(delta)
            positive = [x for x in deltas if x >= -1e-6]
            delta = min(positive) if positive else max(deltas)
            lo, hi = limits.get(joint, (-math.pi, math.pi))
            angles[joint] = min(hi, max(lo, angles[joint] + delta))

    if warm_start is not None and max_step is not None:
        for b in dof:
            prev = warm_start.get(b)
            if prev is not None and b in angles:
                angles[b] = min(prev + max_step, max(prev - max_step, angles[b]))

    basis = _bases_from_angles(
        chain, dof, base_parent_world, rest_local, axis, angles, pre_basis
    )
    return basis, angles


def calibrate_axis_sign(
    chain: list[str],
    dof: list[str],
    base_parent_world: Transform,
    rest_local: dict[str, Transform],
    target_tip: Vector3,
    axis: Vector3,
    *,
    pre_basis: dict[str, Transform] | None = None,
    probe: float = 0.35,
) -> float:
    """+1.0 or -1.0: the hinge orientation whose positive curl approaches the target.

    Probes a small positive curl at the base joint about ``+axis`` and ``-axis``
    and keeps the direction that reduces tip error — self-calibrating, so no
    handedness convention can flip a finger (or the thumb) the wrong way.
    """
    best_sign, best_err = 1.0, math.inf
    for sign in (1.0, -1.0):
        probe_axis = Vector3(axis.x * sign, axis.y * sign, axis.z * sign)
        angles = {b: probe for b in dof}
        basis = _bases_from_angles(
            chain, dof, base_parent_world, rest_local, probe_axis, angles, pre_basis
        )
        posed = _fk(chain, base_parent_world, rest_local, basis)
        err = posed[chain[-1]].translation.distance_to(target_tip)
        if err < best_err:
            best_sign, best_err = sign, err
    return best_sign


def tip_error(
    chain: list[str], base_parent_world: Transform, rest_local: dict[str, Transform],
    basis: dict[str, Transform], target_tip: Vector3,
) -> float:
    """Distance from the solved fingertip to its target (validation metric)."""
    posed = _fk(chain, base_parent_world, rest_local, basis)
    return posed[chain[-1]].translation.distance_to(target_tip)


__all__ = [
    "calibrate_axis_sign",
    "rest_locals",
    "solve_finger",
    "solve_finger_joints",
    "solve_finger_wrap",
    "tip_error",
]
