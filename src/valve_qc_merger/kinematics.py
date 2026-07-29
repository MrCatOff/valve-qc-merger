"""Forward kinematics over an SMD skeleton.

Given a skeleton (``nodes``) and a single frame of local bone poses, compute
each bone's world transform by walking the hierarchy from the roots down.
Bones are resolved parent-before-child regardless of the order they appear in
the file.
"""

from __future__ import annotations

from valve_qc_merger.models.smd import Frame, Node
from valve_qc_merger.transform import Transform


def local_transforms(frame: Frame) -> dict[int, Transform]:
    """Return the local transform of every bone in ``frame``, keyed by bone id."""
    return {
        pose.bone: Transform.from_pos_euler(pose.position, pose.rotation)
        for pose in frame.poses
    }


def world_transforms(nodes: list[Node], frame: Frame) -> dict[int, Transform]:
    """Return the world transform of every bone, keyed by bone id.

    A bone missing from ``frame`` is treated as the identity local transform.
    Parents referenced but absent from ``nodes`` are treated as world roots.
    """
    parent_of = {node.index: node.parent for node in nodes}
    locals_ = local_transforms(frame)
    world: dict[int, Transform] = {}

    def resolve(index: int) -> Transform:
        cached = world.get(index)
        if cached is not None:
            return cached
        local = locals_.get(index, Transform.identity())
        parent = parent_of.get(index, -1)
        result = local if parent < 0 or parent not in parent_of else resolve(parent).compose(local)
        world[index] = result
        return result

    for node in nodes:
        resolve(node.index)
    return world


__all__ = ["local_transforms", "world_transforms"]
