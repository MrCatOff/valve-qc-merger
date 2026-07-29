"""FK retargeting: graft the reference hands onto a weapon's animated skeleton.

Stage-1 strategy (see the command docs):

* The reference hand rides the weapon's wrist bone *rigidly*. My hand bone is
  parented to the weapon wrist with a constant local transform, so the whole
  hand translates and rotates with the gun exactly as the original hand did.
  Its origin sits on the wrist; its orientation keeps the reference's natural
  bind orientation (adjustable later).
* Each finger bends by transferring the weapon finger joint's *local rotation
  delta* (its rotation relative to its own bind pose) onto my finger's bind
  pose. My finger keeps its own bone lengths, so the grip shape is reproduced
  with the reference proportions -- no IK solver in this stage.
* The reference forearm becomes a static stub under the hand (it is mostly
  off-screen in a viewmodel; articulating it is a later Two-Bone IK stage).

The result is a merged skeleton (weapon bones, untouched, plus the grafted hand
bones) and, for every animation, per-frame poses for those grafted bones.
"""

from __future__ import annotations

from dataclasses import dataclass

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
class _ConstantBone:
    """A grafted bone whose local transform never changes (hand, forearm)."""

    merged_index: int
    name: str
    parent: int
    local: Transform


@dataclass(frozen=True, slots=True)
class _FingerBone:
    """A grafted finger joint driven by a weapon joint's local rotation delta."""

    merged_index: int
    name: str
    parent: int
    bind_local: Transform
    source_joint: int
    source_bind_rot_inv: Matrix3


class HandGraft:
    """A resolved plan to merge the reference hands onto a weapon skeleton."""

    def __init__(
        self,
        weapon: Smd,
        hand: Smd,
        links: list[HandLink],
    ) -> None:
        self._weapon = weapon
        self._hand = hand
        self._constants: list[_ConstantBone] = []
        self._fingers: list[_FingerBone] = []
        self._mesh_remap: dict[int, int] = {}
        self._build(links)

    # -- construction ----------------------------------------------------

    def _build(self, links: list[HandLink]) -> None:
        weapon_names = {node.name for node in self._weapon.nodes}
        next_index = max((node.index for node in self._weapon.nodes), default=-1) + 1

        hand_bind = world_transforms(self._hand.nodes, self._hand.frames[0])
        hand_local = {pose.bone: pose for pose in self._hand.frames[0].poses}
        weapon_bind = world_transforms(self._weapon.nodes, self._weapon.frames[0])
        weapon_local = {pose.bone: pose for pose in self._weapon.frames[0].poses}
        hand_name = {node.index: node.name for node in self._hand.nodes}
        hand_parent = {node.index: node.parent for node in self._hand.nodes}

        for link in links:
            forearm_index = hand_parent[link.target_wrist]  # Bip01_?_Forearm

            # Reference hand rides the weapon wrist: origin on the wrist, keeping
            # the reference's own bind orientation.
            wrist_bias = _rotation_bias(
                weapon_bind[link.source_wrist].rotation,
                hand_bind[link.target_wrist].rotation,
            )
            hand_merged = next_index
            next_index += 1
            self._mesh_remap[link.target_wrist] = hand_merged
            self._constants.append(
                _ConstantBone(
                    hand_merged,
                    hand_name[link.target_wrist],
                    link.source_wrist,
                    Transform(wrist_bias, _ZERO),
                )
            )

            # Forearm: static stub under the hand, keeping its bind pose relative
            # to the hand as authored in the reference SMD.
            forearm_local = hand_bind[link.target_wrist].inverse().compose(
                hand_bind[forearm_index]
            )
            forearm_merged = next_index
            next_index += 1
            self._mesh_remap[forearm_index] = forearm_merged
            self._constants.append(
                _ConstantBone(
                    forearm_merged,
                    hand_name[forearm_index],
                    hand_merged,
                    forearm_local,
                )
            )

            # Fingers: three joints each, driven by the weapon joint deltas.
            for source_chain, target_chain in link.finger_pairs:
                parent_merged = hand_merged
                for source_joint, target_joint in zip(
                    source_chain.joints, target_chain.joints, strict=True
                ):
                    bind_local = _pose_transform(hand_local[target_joint])
                    source_bind = _pose_transform(weapon_local[source_joint])
                    merged = next_index
                    next_index += 1
                    self._mesh_remap[target_joint] = merged
                    self._fingers.append(
                        _FingerBone(
                            merged,
                            hand_name[target_joint],
                            parent_merged,
                            bind_local,
                            source_joint,
                            mat3_transpose(source_bind.rotation),
                        )
                    )
                    parent_merged = merged

        grafted_names = {bone[1] for bone in self._graft_bones()}
        if weapon_names & grafted_names:
            raise ValueError("grafted hand bone name collides with a weapon bone name")

    # -- outputs ---------------------------------------------------------

    def merged_nodes(self) -> list[Node]:
        """Weapon nodes plus the grafted hand bones, in merged index order."""
        nodes = list(self._weapon.nodes)
        for bone in self._graft_bones():
            nodes.append(Node(bone[0], bone[1], bone[2]))
        return nodes

    def _graft_bones(self) -> list[tuple[int, str, int]]:
        bones = [(c.merged_index, c.name, c.parent) for c in self._constants]
        bones += [(f.merged_index, f.name, f.parent) for f in self._fingers]
        bones.sort(key=lambda item: item[0])
        return bones

    def _bind_poses(self) -> list[BonePose]:
        poses: list[BonePose] = []
        for bone in self._constants:
            poses.append(_transform_pose(bone.merged_index, bone.local))
        for finger in self._fingers:
            poses.append(_transform_pose(finger.merged_index, finger.bind_local))
        poses.sort(key=lambda pose: pose.bone)
        return poses

    def reference_smd(self) -> Smd:
        """The hand reference SMD: merged skeleton, bind pose and remapped mesh."""
        weapon_bind = {pose.bone: pose for pose in self._weapon.frames[0].poses}
        bind_poses = [weapon_bind[node.index] for node in self._weapon.nodes]
        bind_poses += self._bind_poses()
        triangles = self._remap_mesh()
        return Smd(
            version=self._hand.version,
            nodes=self.merged_nodes(),
            frames=[Frame(0, tuple(bind_poses))],
            triangles=triangles,
        )

    def _remap_mesh(self) -> list[Triangle]:
        triangles: list[Triangle] = []
        for triangle in self._hand.triangles:
            v0, v1, v2 = (self._remap_vertex(vertex) for vertex in triangle.vertices)
            triangles.append(Triangle(triangle.material, (v0, v1, v2)))
        return triangles

    def _remap_vertex(self, vertex: Vertex) -> Vertex:
        return Vertex(
            bone=self._mesh_remap[vertex.bone],
            position=vertex.position,
            normal=vertex.normal,
            uv=vertex.uv,
        )

    def retarget_animation(self, animation: Smd) -> Smd:
        """Return ``animation`` with grafted hand poses added to every frame."""
        frames: list[Frame] = []
        for frame in animation.frames:
            source = {pose.bone: pose for pose in frame.poses}
            poses = list(frame.poses)
            for bone in self._constants:
                poses.append(_transform_pose(bone.merged_index, bone.local))
            for finger in self._fingers:
                poses.append(self._finger_pose(finger, source))
            frames.append(Frame(frame.time, tuple(poses)))
        return Smd(version=animation.version, nodes=self.merged_nodes(), frames=frames)

    @staticmethod
    def _finger_pose(finger: _FingerBone, source: dict[int, BonePose]) -> BonePose:
        source_pose = source.get(finger.source_joint)
        if source_pose is None:
            return _transform_pose(finger.merged_index, finger.bind_local)
        delta = mat3_multiply(finger.source_bind_rot_inv, euler_to_matrix(source_pose.rotation))
        rotation = mat3_multiply(finger.bind_local.rotation, delta)
        return _transform_pose(
            finger.merged_index, Transform(rotation, finger.bind_local.translation)
        )


def _pose_transform(pose: BonePose) -> Transform:
    return Transform.from_pos_euler(pose.position, pose.rotation)


def _transform_pose(bone: int, transform: Transform) -> BonePose:
    return BonePose(bone=bone, position=transform.translation, rotation=transform.to_euler())


def _rotation_bias(source_rot: Matrix3, target_rot: Matrix3) -> Matrix3:
    """Rotation that carries ``source`` orientation onto ``target`` orientation."""
    return mat3_multiply(mat3_transpose(source_rot), target_rot)


__all__ = ["HandGraft"]
