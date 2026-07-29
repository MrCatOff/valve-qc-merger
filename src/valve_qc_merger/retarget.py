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

Finger retargeting works in *world space*: each reference finger joint reproduces
the weapon joint's world orientation (via a constant bind-time alignment),
keeping the reference finger's own bone lengths. Because the reference hand
coincides with the weapon wrist, this reproduces the grip pose regardless of the
two rigs' differing bone-local axes -- a plain local-rotation copy would bend the
fingers around the wrong axes.
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
    clamp_rotation,
    mat3_multiply,
    mat3_transpose,
    rotation_between,
)

_ZERO = Vector3(0.0, 0.0, 0.0)


@dataclass(frozen=True, slots=True)
class _KeptBone:
    """A weapon bone carried into the output (optionally re-parented)."""

    new_index: int
    old_index: int
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
    """A reference finger joint.

    During animation the joint is *aimed*: its own bone (the direction toward
    its child) is rotated to point along the weapon finger bone's world
    direction, using the reference finger's own length (``bind_local``'s
    translation). In the reference/bind pose the joint keeps its authored
    orientation (``bind_local``) so the mesh stays consistently skinned.
    """

    new_index: int
    name: str
    parent: int
    source_joint: int
    bind_local: Transform
    # This bone's own direction to its child in local space; ``None`` for the
    # finger tip (a leaf), which keeps its bind orientation.
    child_dir: Vector3 | None
    # The weapon segment (``source_joint`` -> ``source_child``) this bone aims
    # along. For joints past a shorter weapon finger's end, this is the weapon
    # finger's *last* segment, so the surplus length continues along the weapon
    # finger direction instead of folding back toward the tip.
    source_child: int | None
    # Maps the weapon bone's world orientation onto this bone's output-bind
    # orientation, so the finger's roll (twist about its axis) is taken from the
    # weapon -- which gripped correctly -- while its direction comes from the aim.
    orient_align: Matrix3


@dataclass(frozen=True, slots=True)
class _FingerChain:
    """A grafted finger as an ordered chain, with its weapon fingertip target."""

    base: int  # output index of the chain's parent (the hand)
    joints: tuple[int, ...]  # output indices, root..tip
    weapon_tip: int  # weapon fingertip index the tip should reach


@dataclass
class _SidePlan:
    kept: list[_KeptBone] = field(default_factory=list)
    hands: list[_HandBone] = field(default_factory=list)
    constants: list[_ConstantBone] = field(default_factory=list)
    fingers: list[_FingerBone] = field(default_factory=list)
    chains: list[_FingerChain] = field(default_factory=list)


class HandGraft:
    """A resolved plan to replace a weapon's hands with the reference hands."""

    def __init__(
        self,
        weapon: Smd,
        hand: Smd,
        links: list[HandLink],
        offsets: dict[str, Transform] | None = None,
        weapon_offset: Vector3 = _ZERO,
        finger_ik: bool = False,
    ) -> None:
        self._weapon = weapon
        self._hand = hand
        self._offsets = offsets or {}
        self._weapon_offset = weapon_offset
        self._finger_ik = finger_ik
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
                _KeptBone(old_to_new[node.index], node.index, node.name, parent_new, compensation)
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

        # Fingers: each of my joints aims its bone along the matching weapon
        # finger segment. The weapon finger may have fewer bones than mine (a
        # short thumb); joints past the weapon finger's end keep their bind pose.
        # Roll is taken from the weapon bone via ``orient_align``.
        transform_rot = mat3_multiply(
            target_frame.rotation, mat3_transpose(hand_bind[link.target_wrist].rotation)
        )
        cursor = base_index + 2
        for source_chain, target_chain in link.finger_pairs:
            parent_new = hand_new
            source_joints = source_chain.joints
            target_joints = target_chain.joints
            chain_start = cursor
            for depth, target_joint in enumerate(target_joints):
                source_child: int | None = None
                child_dir: Vector3 | None = None
                source_joint = source_joints[min(depth, len(source_joints) - 1)]
                if depth + 1 < len(target_joints):  # my joint has a child (not the tip)
                    child_dir = hand_local[target_joints[depth + 1]].position
                    if depth + 1 < len(source_joints):
                        source_joint = source_joints[depth]
                        source_child = source_joints[depth + 1]
                    else:
                        # Surplus joint: continue along the weapon finger's last
                        # segment rather than folding back toward its tip.
                        source_joint = source_joints[-2]
                        source_child = source_joints[-1]
                output_bind_rot = mat3_multiply(transform_rot, hand_bind[target_joint].rotation)
                orient_align = mat3_multiply(
                    mat3_transpose(weapon_bind[source_joint].rotation), output_bind_rot
                )
                self._mesh_remap[target_joint] = cursor
                self._mesh_transform[target_joint] = mesh_transform
                self._plan.fingers.append(
                    _FingerBone(
                        cursor,
                        hand_name[target_joint],
                        parent_new,
                        source_joint,
                        _pose_transform(hand_local[target_joint]),
                        child_dir,
                        source_child,
                        orient_align,
                    )
                )
                parent_new = cursor
                cursor += 1
            self._plan.chains.append(
                _FingerChain(hand_new, tuple(range(chain_start, cursor)), source_joints[-1])
            )

        self._mesh_transform[link.target_wrist] = mesh_transform

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
        poses = self._frame_poses(self._weapon.frames[0], aim=False)
        return Smd(
            version=self._hand.version,
            nodes=self.merged_nodes(),
            frames=[Frame(0, tuple(poses))],
            triangles=self._remap_mesh(),
        )

    def weapon_reference_smd(self, source: Smd | None = None) -> Smd:
        """A weapon (gun) reference re-expressed on the merged skeleton.

        Every SMD in the model must share one skeleton. A weapon reference is
        otherwise left with its original hand bones, which would conflict with
        the grafted hand bones when the model is assembled. Here the gun geometry
        (from ``source``, defaulting to the skeleton-source reference) is remapped
        to the kept bones' new indices on the merged skeleton.
        """
        weapon = source if source is not None else self._weapon
        poses = self._frame_poses(self._weapon.frames[0], aim=False)
        triangles: list[Triangle] = []
        for triangle in weapon.triangles:
            remapped = tuple(self._remap_weapon_vertex(vertex) for vertex in triangle.vertices)
            triangles.append(Triangle(triangle.material, remapped))  # type: ignore[arg-type]
        return Smd(
            version=weapon.version,
            nodes=self.merged_nodes(),
            frames=[Frame(0, tuple(poses))],
            triangles=triangles,
        )

    def _remap_weapon_vertex(self, vertex: Vertex) -> Vertex:
        new_bone = self._weapon_kept_pose.get(vertex.bone)
        if new_bone is None:
            raise ValueError(
                f"weapon geometry is skinned to removed hand bone index {vertex.bone}"
            )
        # Push the gun geometry off the hands (baked into the bind, so it follows
        # the weapon bones through the animation).
        position = Vector3(
            vertex.position.x + self._weapon_offset.x,
            vertex.position.y + self._weapon_offset.y,
            vertex.position.z + self._weapon_offset.z,
        )
        return Vertex(
            bone=new_bone,
            position=position,
            normal=vertex.normal,
            uv=vertex.uv,
        )

    def retarget_animation(self, animation: Smd) -> Smd:
        """Return ``animation`` with the weapon hands replaced by the reference hands."""
        frames = [
            Frame(frame.time, tuple(self._frame_poses(frame, aim=True)))
            for frame in animation.frames
        ]
        return Smd(version=animation.version, nodes=self.merged_nodes(), frames=frames)

    def _frame_poses(self, frame: Frame, aim: bool) -> list[BonePose]:
        """Build every output bone's pose for one frame of weapon-bone data.

        With ``aim`` false the fingers keep their bind orientation (used for the
        reference/bind pose so the mesh stays consistently skinned); with ``aim``
        true they point along the weapon finger bones (used for animation).
        """
        source = {pose.bone: pose for pose in frame.poses}
        weapon_world = world_transforms(self._weapon.nodes, frame)
        world_cache: dict[int, Transform] = {}
        poses: list[BonePose] = []

        # Kept weapon bones (compensated where re-parented).
        for bone in self._plan.kept:
            original = source.get(bone.old_index)
            local = _pose_transform(original) if original is not None else Transform.identity()
            poses.append(_transform_pose(bone.new_index, bone.compensation.compose(local)))

        # Reference hand bones: reproduce the weapon wrist local, times the offset.
        for hand in self._plan.hands:
            wrist = source.get(hand.source_wrist)
            wrist_local = _pose_transform(wrist) if wrist is not None else Transform.identity()
            poses.append(_transform_pose(hand.new_index, wrist_local.compose(hand.offset)))
            wrist_world = weapon_world.get(hand.source_wrist, Transform.identity())
            world_cache[hand.new_index] = wrist_world.compose(hand.offset)

        for constant in self._plan.constants:
            poses.append(_transform_pose(constant.new_index, constant.local))

        # Fingers, root->tip (parents precede children in the plan order).
        finger_local: dict[int, Transform] = {}
        for finger in self._plan.fingers:
            parent_world = world_cache[finger.parent]
            local = self._finger_local(finger, parent_world, weapon_world, aim)
            finger_local[finger.new_index] = local
            world_cache[finger.new_index] = parent_world.compose(local)

        # Curl each finger so its tip reaches the weapon fingertip (grip contact).
        if aim and self._finger_ik:
            for chain in self._plan.chains:
                self._solve_finger_ik(chain, finger_local, world_cache, weapon_world)

        for finger in self._plan.fingers:
            poses.append(_transform_pose(finger.new_index, finger_local[finger.new_index]))

        poses.sort(key=lambda pose: pose.bone)
        return poses

    @staticmethod
    def _finger_local(
        finger: _FingerBone,
        parent_world: Transform,
        weapon_world: dict[int, Transform],
        aim: bool,
    ) -> Transform:
        if not aim or finger.child_dir is None or finger.source_child is None:
            return finger.bind_local
        orient = weapon_world.get(finger.source_joint)
        child = weapon_world.get(finger.source_child)
        if orient is None or child is None:
            return finger.bind_local
        # Aim my bone along the weapon finger segment.
        direction_world = _subtract(child.translation, orient.translation)
        if direction_world.length() < 1e-6:
            return finger.bind_local
        # Take the finger's roll from the weapon bone's world orientation (it
        # gripped correctly), then swing that orientation minimally so the bone
        # points along the aim direction. This fixes the direction without
        # leaving the twist unconstrained (which flipped thumbs the wrong way).
        rolled = mat3_multiply(orient.rotation, finger.orient_align)
        rolled_forward = Transform(rolled, _ZERO).rotate_vector(finger.child_dir)
        swing = rotation_between(rolled_forward, direction_world)
        world_rotation = mat3_multiply(swing, rolled)
        local_rotation = mat3_multiply(mat3_transpose(parent_world.rotation), world_rotation)
        return Transform(local_rotation, finger.bind_local.translation)

    @staticmethod
    def _solve_finger_ik(
        chain: _FingerChain,
        finger_local: dict[int, Transform],
        world_cache: dict[int, Transform],
        weapon_world: dict[int, Transform],
        iterations: int = 16,
        tolerance: float = 0.05,
        max_curl: float = 1.2,
    ) -> None:
        """CCD: curl the finger so its tip joint reaches the weapon fingertip.

        Adjusts every joint but the last (which sets only the fingertip's own
        orientation, not the tip position), seeded from the aim pose so the
        finger keeps its direction and only bends enough to reach the grip. Each
        joint's bend away from the aim is capped at ``max_curl`` radians so a
        finger longer than the weapon's does not fold back on itself.
        """
        target = weapon_world.get(chain.weapon_tip)
        if target is None or len(chain.joints) < 2:
            return
        goal = target.translation
        base = world_cache[chain.base]
        locals_ = [finger_local[i] for i in chain.joints]
        aim_rotation = [local.rotation for local in locals_]

        def forward() -> list[Transform]:
            worlds = [base.compose(locals_[0])]
            for k in range(1, len(locals_)):
                worlds.append(worlds[k - 1].compose(locals_[k]))
            return worlds

        worlds = forward()
        for _ in range(iterations):
            tip = worlds[-1].translation
            if _subtract(tip, goal).length() < tolerance:
                break
            for j in range(len(locals_) - 2, -1, -1):
                joint = worlds[j]
                to_tip = _subtract(tip, joint.translation)
                to_goal = _subtract(goal, joint.translation)
                if to_tip.length() < 1e-6 or to_goal.length() < 1e-6:
                    continue
                swing = rotation_between(to_tip, to_goal)
                parent_rot = base.rotation if j == 0 else worlds[j - 1].rotation
                new_world_rot = mat3_multiply(swing, joint.rotation)
                local_rot = mat3_multiply(mat3_transpose(parent_rot), new_world_rot)
                # Keep the bend natural: cap how far this joint turns from the aim.
                curl = clamp_rotation(
                    mat3_multiply(mat3_transpose(aim_rotation[j]), local_rot), max_curl
                )
                locals_[j] = Transform(mat3_multiply(aim_rotation[j], curl), locals_[j].translation)
                worlds = forward()
                tip = worlds[-1].translation

        for k, index in enumerate(chain.joints):
            finger_local[index] = locals_[k]
            world_cache[index] = worlds[k]

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


def _subtract(a: Vector3, b: Vector3) -> Vector3:
    return Vector3(a.x - b.x, a.y - b.y, a.z - b.z)


def _normalize(v: Vector3) -> Vector3:
    length = v.length()
    return v if length == 0 else Vector3(v.x / length, v.y / length, v.z / length)


def _pose_transform(pose: BonePose) -> Transform:
    return Transform.from_pos_euler(pose.position, pose.rotation)


def _transform_pose(bone: int, transform: Transform) -> BonePose:
    return BonePose(bone=bone, position=transform.translation, rotation=transform.to_euler())


__all__ = ["HandGraft"]
