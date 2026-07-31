"""Text-level Euler continuity unwrap for emitted animation SMDs (§7.8), pure.

BST re-derives every bone's Euler from the pose matrix at export time
(``PoseMatrix.to_euler()``), independently per frame, so it can wrap by 2pi or
gimbal-flip across the |Y|=90 deg singularity even when the rotation itself moves
only a little. Since ``bpy.ops.graph.euler_filter()`` needs a Graph Editor context
that does not exist under ``--background``, the spec enforces continuity here, on
the written text: for each bone, walk frames and pick — among the principal Euler,
its gimbal-equivalent, and their 2pi wraps — the representation nearest the
previous frame. The rotation each frame is unchanged; only its naming is made
continuous, which is what GoldSrc's per-component interpolation needs.
"""

from __future__ import annotations

import math

from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Smd

_TWO_PI = 2.0 * math.pi


def _wrap_near(value: float, reference: float) -> float:
    """The 2pi-equivalent of ``value`` closest to ``reference``."""
    return value + _TWO_PI * round((reference - value) / _TWO_PI)


def _gimbal_flip(euler: Vector3) -> Vector3:
    """The other XYZ-Euler naming the same rotation: (x+pi, pi-y, z+pi)."""
    return Vector3(euler.x + math.pi, math.pi - euler.y, euler.z + math.pi)


def _nearest_representation(euler: Vector3, prev: Vector3) -> Vector3:
    """Whichever of the two equivalent Eulers (each 2pi-wrapped) is closest to prev."""
    best: Vector3 | None = None
    best_cost = math.inf
    for candidate in (euler, _gimbal_flip(euler)):
        wrapped = Vector3(
            _wrap_near(candidate.x, prev.x),
            _wrap_near(candidate.y, prev.y),
            _wrap_near(candidate.z, prev.z),
        )
        cost = ((wrapped.x - prev.x) ** 2
                + (wrapped.y - prev.y) ** 2
                + (wrapped.z - prev.z) ** 2)
        if cost < best_cost:
            best, best_cost = wrapped, cost
    assert best is not None
    return best


def unwrap_smd(smd: Smd) -> tuple[Smd, int]:
    """Return a copy of ``smd`` with continuous per-bone Euler tracks + change count.

    A component is counted as changed when it moves by more than a hair from the
    value BST wrote (i.e. a wrap or flip was actually applied).
    """
    prev: dict[int, Vector3] = {}
    changed = 0
    new_frames: list[Frame] = []
    for frame in smd.frames:
        new_poses: list[BonePose] = []
        for pose in frame.poses:
            last = prev.get(pose.bone)
            if last is None:
                rotation = pose.rotation
            else:
                rotation = _nearest_representation(pose.rotation, last)
                if (abs(rotation.x - pose.rotation.x) > 1e-6
                        or abs(rotation.y - pose.rotation.y) > 1e-6
                        or abs(rotation.z - pose.rotation.z) > 1e-6):
                    changed += 1
            prev[pose.bone] = rotation
            new_poses.append(BonePose(pose.bone, pose.position, rotation))
        new_frames.append(Frame(frame.time, tuple(new_poses)))
    unwrapped = Smd(
        version=smd.version, nodes=list(smd.nodes),
        frames=new_frames, triangles=list(smd.triangles),
    )
    return unwrapped, changed


__all__ = ["unwrap_smd"]
