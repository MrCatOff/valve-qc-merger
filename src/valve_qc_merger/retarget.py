"""Replace a weapon's hand bones with the reference hands.

This is a *replacement*, not an addition. The weapon's own wrist and finger
bones are removed and the reference hand bones take their place, so the bone
count does not balloon (the original weapon hands are gone).

What is kept, removed and re-parented
-------------------------------------
* **Removed** -- the weapon wrist bones and every finger-chain bone (they are
  what the reference hands replace, and none of them carry gun geometry).
* **Kept** -- the weapon root and all gun/structural bones (the gun mesh is
  skinned to some of these, e.g. the palm bone the revolver hangs from).
* **Re-parented** -- a kept bone whose parent was a removed wrist (the gun palm,
  the ``Se_Hand`` helpers) is re-attached to the corresponding reference hand
  bone, so the gun keeps riding the hand.

World-preserving graft with an alignment offset
-----------------------------------------------
The reference hand bone reproduces the weapon wrist's world transform every
frame, composed with a constant per-hand ``offset``. That offset lets you slide
and rotate the reference hand onto the grip without moving the gun: any kept
bone re-parented under the hand is compensated by ``offset⁻¹``, so its world
motion is identical regardless of the offset. With the default (identity) offset
the gun animates exactly as authored and the hand rides the wrist precisely.

Fingers bend by transferring each weapon finger joint's local rotation delta
onto the reference finger's bind pose (Stage-1 FK retargeting; no IK solver).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from valve_qc_merger.correspondence import HandLink
from valve_qc_merger.kinematics import world_transforms
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd, Triangle, Vertex
from valve_qc_merger.transform import (
    Matrix3,
    Transform,
    euler_to_matrix,
    mat3_multiply,
    mat3_transpose,
)

_ZERO = Vector3(0.0, 0.0, 0.0)


@dataclass(frozen=True, slots=True)
class _KeptBone:
    """A weapon bone carried into the output (optionally re-parented)."""

    new_index: int
    name: str
    parent: int
    # When re-parented under a reference hand, the compensating offset inverse
    # is applied so the bone's world motion is preserved; otherwise identity.
    compensation: Transform


@dataclass(frozen=True, slots=True)
class _HandBone:
    """The reference wrist bone, reproducing the weapon wrist plus an offset."""

    new_index: int
    name: str
    parent: int
    source_wrist: int
    offset: Transform


@dataclass(frozen=True, slots=True)
class _ConstantBone:
    """A reference bone with a fixed local transform (the forearm stub)."""

    new_index: int
    name: str
    parent: int
    local: Transform


@dataclass(frozen=True, slots=True)
class _FingerBone:
    """A reference finger joint driven by a weapon joint's rotation delta."""

    new_index: int
    name: str
    parent: int
    bind_local: Transform
    source_joint: int
    source_bind_rot_inv: Matrix3


@dataclass
class _SidePlan:
    kept: list[_KeptBone] = field(default_factory=list)
    hands: list[_HandBone] = field(default_factory=list)
    constants: list[_ConstantBone] = field(default_factory=list)
    fingers: list[_FingerBone] = field(default_factory=list)


class HandGraft:
    """A resolved plan to replace a weapon's hands with the reference hands."""

    def __init__(
        self,
        weapon: Smd,
        hand: Smd,
        links: list[HandLink],
        offsets: dict[str, Transform] | None = None,
    ) -> None:
        self._weapon = weapon
        self._hand = hand
        self._offsets = offsets or {}
        self._plan = _SidePlan()
        self._mesh_remap: dict[int, int] = {}
        self._mesh_transform: dict[int, Transform] = {}
        self._weapon_kept_pose: dict[int, int] = {}  # old weapon index -> new index
        self._build(links)

    # -- construction ----------------------------------------------------

    def _build(self, links: list[HandLink]) -> None:
        weapon_parent = {node.index: node.parent for node in self._weapon.nodes}
        wrist_side = {link.source_wrist: link.side for link in links}
        removed = self._removed_bones(links)

        # Assign new indices: kept weapon bones first, in original order.
        old_to_new: dict[int, int] = {}
        next_index = 0
        for node in self._weapon.nodes:
            if node.index in removed:
                continue
            old_to_new[node.index] = next_index
            next_index += 1
        self._weapon_kept_pose = old_to_new

        # Reference hand bones per side get the indices after the kept bones.
        hand_index_by_side: dict[str, int] = {}
        for link in links:
            hand_index_by_side[link.side] = next_index
            next_index += 1 + 1 + 3 * len(link.finger_pairs)  # hand + forearm + fingers

        weapon_bind = world_transforms(self._weapon.nodes, self._weapon.frames[0])
        hand_bind = world_transforms(self._hand.nodes, self._hand.frames[0])
        hand_local = {pose.bone: pose for pose in self._hand.frames[0].poses}
        hand_name = {node.index: node.name for node in self._hand.nodes}
        hand_parent = {node.index: node.parent for node in self._hand.nodes}

        # Kept weapon bones (with re-parenting + world-preserving compensation).
        for node in self._weapon.nodes:
            if node.index in removed:
                continue
            parent = node.parent
            compensation = Transform.identity()
            if parent in removed:
                side = wrist_side.get(parent)
                if side is None:
                    raise ValueError(
                        f"kept bone {node.name!r} hangs off a removed finger bone"
                    )
                parent_new = hand_index_by_side[side]
                compensation = self._offset(side).inverse()
            else:
                parent_new = old_to_new.get(parent, -1)
            self._plan.kept.append(
                _KeptBone(old_to_new[node.index], node.name, parent_new, compensation)
            )

        # Reference hand bones per side.
        for link in links:
            self._build_side(
                link,
                hand_index_by_side[link.side],
                old_to_new,
                weapon_parent,
                weapon_bind,
                hand_bind,
                hand_local,
                hand_name,
                hand_parent,
            )

    @staticmethod
    def _removed_bones(links: list[HandLink]) -> set[int]:
        removed: set[int] = set()
        for link in links:
            removed.add(link.source_wrist)
            for source_chain, _ in link.finger_pairs:
                removed.update(source_chain.joints)
        return removed

    def _offset(self, side: str) -> Transform:
        return self._offsets.get(side, Transform.identity())

    def _build_side(
        self,
        link: HandLink,
        base_index: int,
        old_to_new: dict[int, int],
        weapon_parent: dict[int, int],
        weapon_bind: dict[int, Transform],
        hand_bind: dict[int, Transform],
        hand_local: dict[int, BonePose],
        hand_name: dict[int, str],
        hand_parent: dict[int, int],
    ) -> None:
        offset = self._offset(link.side)
        forearm_index = hand_parent[link.target_wrist]

        # The reference hand replaces the wrist: same parent as the old wrist.
        wrist_parent = weapon_parent[link.source_wrist]
        hand_parent_new = old_to_new.get(wrist_parent, -1)
        hand_new = base_index
        self._mesh_remap[link.target_wrist] = hand_new
        self._plan.hands.append(
            _HandBone(
                hand_new,
                hand_name[link.target_wrist],
                hand_parent_new,
                link.source_wrist,
                offset,
            )
        )

        # Rigid transform that carries the reference hand (and its mesh) from the
        # reference's own space onto the weapon wrist (times the offset).
        target_frame = weapon_bind[link.source_wrist].compose(offset)
        mesh_transform = target_frame.compose(hand_bind[link.target_wrist].inverse())

        # Forearm: static stub under the hand, keeping its reference-space pose
        # relative to the hand.
        forearm_local = hand_bind[link.target_wrist].inverse().compose(
            hand_bind[forearm_index]
        )
        forearm_new = base_index + 1
        self._mesh_remap[forearm_index] = forearm_new
        self._mesh_transform[forearm_index] = mesh_transform
        self._plan.constants.append(
            _ConstantBone(forearm_new, hand_name[forearm_index], hand_new, forearm_local)
        )

        # Fingers.
        cursor = base_index + 2
        for source_chain, target_chain in link.finger_pairs:
            parent_new = hand_new
            for source_joint, target_joint in zip(
                source_chain.joints, target_chain.joints, strict=True
            ):
                bind_local = _pose_transform(hand_local[target_joint])
                source_bind = _pose_transform(
                    {p.bone: p for p in self._weapon.frames[0].poses}[source_joint]
                )
                self._mesh_remap[target_joint] = cursor
                self._plan.fingers.append(
                    _FingerBone(
                        cursor,
                        hand_name[target_joint],
                        parent_new,
                        bind_local,
                        source_joint,
                        mat3_transpose(source_bind.rotation),
                    )
                )
                parent_new = cursor
                cursor += 1

        # Every reference-hand vertex on this side is relocated by mesh_transform.
        self._mesh_transform[link.target_wrist] = mesh_transform
        for _, target_chain in link.finger_pairs:
            for joint in target_chain.joints:
                self._mesh_transform[joint] = mesh_transform

    # -- outputs ---------------------------------------------------------

    def removed_count(self) -> int:
        """Number of weapon hand bones removed from the skeleton."""
        return len(self._weapon.nodes) - len(self._plan.kept)

    def added_count(self) -> int:
        """Number of reference hand bones added to the skeleton."""
        return len(self._plan.hands) + len(self._plan.constants) + len(self._plan.fingers)

    def merged_nodes(self) -> list[Node]:
        """The output skeleton: kept weapon bones plus the reference hand bones."""
        nodes = [(b.new_index, b.name, b.parent) for b in self._plan.kept]
        nodes += [(b.new_index, b.name, b.parent) for b in self._plan.hands]
        nodes += [(b.new_index, b.name, b.parent) for b in self._plan.constants]
        nodes += [(b.new_index, b.name, b.parent) for b in self._plan.fingers]
        nodes.sort(key=lambda item: item[0])
        return [Node(index, name, parent) for index, name, parent in nodes]

    def reference_smd(self) -> Smd:
        """The hand reference SMD: output skeleton, bind pose and relocated mesh."""
        weapon_bind = {pose.bone: pose for pose in self._weapon.frames[0].poses}
        poses = self._frame_poses(weapon_bind)
        return Smd(
            version=self._hand.version,
            nodes=self.merged_nodes(),
            frames=[Frame(0, tuple(poses))],
            triangles=self._remap_mesh(),
        )

    def retarget_animation(self, animation: Smd) -> Smd:
        """Return ``animation`` with the weapon hands replaced by the reference hands."""
        frames: list[Frame] = []
        for frame in animation.frames:
            source = {pose.bone: pose for pose in frame.poses}
            frames.append(Frame(frame.time, tuple(self._frame_poses(source))))
        return Smd(version=animation.version, nodes=self.merged_nodes(), frames=frames)

    def _frame_poses(self, source: dict[int, BonePose]) -> list[BonePose]:
        """Build every output bone's pose for one frame of weapon-bone data."""
        poses: list[BonePose] = []
        source_by_new = {new: source.get(old) for old, new in self._weapon_kept_pose.items()}
        # Kept weapon bones (compensated where re-parented).
        for bone in self._plan.kept:
            original = source_by_new.get(bone.new_index)
            local = _pose_transform(original) if original is not None else Transform.identity()
            poses.append(_transform_pose(bone.new_index, bone.compensation.compose(local)))
        # Reference hand bones: reproduce the weapon wrist local, times the offset.
        for hand in self._plan.hands:
            wrist = source.get(hand.source_wrist)
            wrist_local = _pose_transform(wrist) if wrist is not None else Transform.identity()
            poses.append(_transform_pose(hand.new_index, wrist_local.compose(hand.offset)))
        for constant in self._plan.constants:
            poses.append(_transform_pose(constant.new_index, constant.local))
        for finger in self._plan.fingers:
            poses.append(self._finger_pose(finger, source))
        poses.sort(key=lambda pose: pose.bone)
        return poses

    @staticmethod
    def _finger_pose(finger: _FingerBone, source: dict[int, BonePose]) -> BonePose:
        source_pose = source.get(finger.source_joint)
        if source_pose is None:
            return _transform_pose(finger.new_index, finger.bind_local)
        delta = mat3_multiply(finger.source_bind_rot_inv, euler_to_matrix(source_pose.rotation))
        rotation = mat3_multiply(finger.bind_local.rotation, delta)
        return _transform_pose(
            finger.new_index, Transform(rotation, finger.bind_local.translation)
        )

    def _remap_mesh(self) -> list[Triangle]:
        triangles: list[Triangle] = []
        for triangle in self._hand.triangles:
            v0, v1, v2 = (self._remap_vertex(vertex) for vertex in triangle.vertices)
            triangles.append(Triangle(triangle.material, (v0, v1, v2)))
        return triangles

    def _remap_vertex(self, vertex: Vertex) -> Vertex:
        transform = self._mesh_transform[vertex.bone]
        return Vertex(
            bone=self._mesh_remap[vertex.bone],
            position=transform.transform_point(vertex.position),
            normal=transform.rotate_vector(vertex.normal),
            uv=vertex.uv,
        )


def _pose_transform(pose: BonePose) -> Transform:
    return Transform.from_pos_euler(pose.position, pose.rotation)


def _transform_pose(bone: int, transform: Transform) -> BonePose:
    return BonePose(bone=bone, position=transform.translation, rotation=transform.to_euler())


__all__ = ["HandGraft"]
