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

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from statistics import median, pvariance

from valve_qc_merger.correspondence import HandLink
from valve_qc_merger.kinematics import world_transforms
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd, Triangle, Vertex
from valve_qc_merger.transform import (
    Matrix3,
    Transform,
    axis_angle,
    clamp_rotation,
    mat3_multiply,
    mat3_transpose,
    rotation_between,
)

_ZERO = Vector3(0.0, 0.0, 0.0)
_GRIP_BONE_SAMPLE = 200  # gun verts nearest a hand whose dominant bone it rides
_SEAT_ITERATIONS = 6  # closest-point seating passes (converges in 2-3 when close)
_SEAT_TOLERANCE = 0.25  # stop seating once a pass moves less than this


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
    # Extra flexion (radians) to fold this finger deeper than the weapon authored
    # -- used to tuck the trigger finger further into the guard. 0 for fingers
    # left at the authored grip.
    curl: float = 0.0
    # Whether this finger tracks a ``grip_slide`` (the wrapping fingers follow the
    # gun as it slides forward; the thumb does not, so the moving body clears it).
    follow: bool = True
    # Pull the finger's grip contact back toward the hand by this many units, so a
    # reference finger whose *mesh* is longer than its bones stops poking through
    # the weapon. Set per finger by the collision solver.
    retract: float = 0.0


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
        index_curl: float = 0.0,
        grip_slide: Vector3 = _ZERO,
        retracts: dict[str, float] | None = None,
    ) -> None:
        self._weapon = weapon
        self._hand = hand
        self._offsets = offsets or {}
        self._weapon_offset = weapon_offset
        self._grip_slide = grip_slide
        self._retracts = retracts or {}
        self._finger_ik = finger_ik
        self._index_curl = index_curl
        self._plan = _SidePlan()
        self._mesh_remap: dict[int, int] = {}
        self._mesh_transform: dict[int, Transform] = {}
        self._weapon_kept_pose: dict[int, int] = {}  # old weapon index -> new index
        # Output (merged) hand-mesh bone -> the weapon bone that drives it, so the
        # reference mesh can be re-skinned onto the weapon's own skeleton.
        self._merged_to_weapon: dict[int, int] = {}
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
        self._merged_to_weapon[hand_new] = link.source_wrist
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
        # The reference forearm is a stub with no weapon counterpart (the weapon
        # wrist's parent is a gun/structural bone elsewhere); ride the wrist so the
        # forearm mesh stays attached to the hand instead of tearing off it.
        self._merged_to_weapon[forearm_new] = link.source_wrist
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
                self._merged_to_weapon[cursor] = source_joints[min(depth, len(source_joints) - 1)]
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
            base_name = hand_name[target_joints[0]]
            is_index = base_name == f"Bip01_{link.side}_Finger1"
            is_thumb = base_name == f"Bip01_{link.side}_Finger0"
            self._plan.chains.append(
                _FingerChain(
                    hand_new,
                    tuple(range(chain_start, cursor)),
                    source_joints[-1],
                    self._index_curl if is_index else 0.0,
                    not is_thumb,
                    self._retracts.get(base_name, 0.0),
                )
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

    def weight_transferred_smd(self, body_bones: set[int] | None = None) -> Smd:
        """The reference hand mesh re-skinned onto the *weapon's own* skeleton.

        Instead of adding reference bones (the graft), the reference hand is posed
        into the weapon's grip, seated onto the gun handle, and bound rigidly to the
        gun's grip bone so the weapon's own animations drive it with no retargeting
        -- and, being one rigid piece per side, it can never tear. ``body_bones``,
        if given, restricts grip binding to the gun's rigid body (see
        :func:`rigid_weapon_bones`) so a hand never rides a bullet or the slide.
        Output is at the weapon's reference (grip) pose.
        """
        # The graft's placed (but flat) reference mesh, on the merged skeleton.
        placed = self.reference_smd()
        bind = Frame(0, tuple(self._frame_poses(self._weapon.frames[0], aim=False)))
        grip = Frame(0, tuple(self._frame_poses(self._weapon.frames[0], aim=True)))
        bind_world = world_transforms(self.merged_nodes(), bind)
        grip_world = world_transforms(self.merged_nodes(), grip)
        weapon_bind = world_transforms(self._weapon.nodes, self._weapon.frames[0])
        merged_side = {n.index: ("L" if "_L_" in n.name else "R") for n in self.merged_nodes()}
        wrist = {("L" if "_L_" in h.name else "R"): h.source_wrist for h in self._plan.hands}
        default_wrist = next(iter(wrist.values()))

        # Pose each reference vertex from the flat bind into the grip (this curls
        # the fingers) as a model-space delta.
        def gripped(vertex: Vertex) -> Vector3:
            to_grip = grip_world[vertex.bone].compose(bind_world[vertex.bone].inverse())
            return to_grip.transform_point(vertex.position)

        # The finger bones aim at the weapon's finger *bones*, which sit inside the
        # gun; slide each hand onto the handle *surface* so it actually grips.
        seat = self._grip_seat(placed, gripped, weapon_bind, merged_side)
        # RIGID GRIP: bind each hand to the bone the gun's grip rides, so the two
        # move as one -- the correspondence wrist can move differently and drift off.
        grip_bones = self._grip_bones(placed, gripped, weapon_bind, merged_side, body_bones)

        triangles: list[Triangle] = []
        for triangle in placed.triangles:
            verts: list[Vertex] = []
            for vertex in triangle.vertices:
                side = merged_side.get(vertex.bone, "?")
                weapon_bone = grip_bones.get(side, wrist.get(side, default_wrist))
                world_p = gripped(vertex)
                nudge = seat.get(side)
                if nudge is not None:
                    world_p = Vector3(
                        world_p.x + nudge.x, world_p.y + nudge.y, world_p.z + nudge.z
                    )
                to_grip = grip_world[vertex.bone].compose(bind_world[vertex.bone].inverse())
                normal_rot = weapon_bind[weapon_bone].inverse().compose(to_grip)
                verts.append(
                    Vertex(
                        bone=weapon_bone,
                        position=weapon_bind[weapon_bone].inverse().transform_point(world_p),
                        normal=normal_rot.rotate_vector(vertex.normal),
                        uv=vertex.uv,
                    )
                )
            triangles.append(Triangle(triangle.material, (verts[0], verts[1], verts[2])))
        return Smd(
            version=self._hand.version,
            nodes=self._weapon.nodes,
            frames=[self._weapon.frames[0]],
            triangles=triangles,
        )

    def _grip_bones(
        self,
        placed: Smd,
        gripped: Callable[[Vertex], Vector3],
        weapon_bind: dict[int, Transform],
        merged_side: dict[int, str],
        body_bones: set[int] | None = None,
    ) -> dict[str, int]:
        """Per-side weapon bone that carries the gun's grip nearest each hand.

        The hand must ride the same bone the gun's grip does; the correspondence
        wrist can be a different bone that a fast draw or reload swings by a
        different amount, leaving the hand floating while the gun animates away
        (some rigs even have near-duplicate bone names, e.g. ``Bone 04`` beside
        ``Bone04``, and the wrong one gets picked). For each side this bins the gun
        vertices nearest the hand and returns their dominant bone -- the bone the
        handle hangs off -- so hand and grip stay locked through every frame.
        """
        gun = [
            (v.bone, weapon_bind[v.bone].transform_point(v.position))
            for triangle in self._weapon.triangles
            for v in triangle.vertices
            if body_bones is None or v.bone in body_bones
        ]
        names = {node.index: node.name for node in self.merged_nodes()}
        by_side: dict[str, list[Vector3]] = defaultdict(list)
        for triangle in placed.triangles:
            for vertex in triangle.vertices:
                side = merged_side.get(vertex.bone)
                if side in ("L", "R") and "Forearm" not in names.get(vertex.bone, ""):
                    by_side[side].append(gripped(vertex))

        bones: dict[str, int] = {}
        for side, points in by_side.items():
            cx = sum(p.x for p in points) / len(points)
            cy = sum(p.y for p in points) / len(points)
            cz = sum(p.z for p in points) / len(points)
            nearest = sorted(
                gun, key=lambda e: (e[1].x - cx) ** 2 + (e[1].y - cy) ** 2 + (e[1].z - cz) ** 2
            )[:_GRIP_BONE_SAMPLE]
            bones[side] = Counter(bone for bone, _ in nearest).most_common(1)[0][0]
        return bones

    def _grip_seat(
        self,
        placed: Smd,
        gripped: Callable[[Vertex], Vector3],
        weapon_bind: dict[int, Transform],
        merged_side: dict[int, str],
    ) -> dict[str, Vector3]:
        """Per-side world translation that seats the hand's grip on the gun.

        The finger bones aim at the weapon's own finger bones, which sit *inside*
        the gun shell, and some rigs park the wrist well off the grip, so the
        reference-hand mesh can rest a good way from the handle surface. For each
        side this takes the finger/palm vertices (never the forearm), measures each
        one's displacement to the nearest point on the gun, and returns the median
        of the third that sit closest -- the slide that lands the grip on the gun,
        driven by the fingers that actually wrap it rather than the whole arm.
        """
        gun_pts = [
            weapon_bind[v.bone].transform_point(v.position)
            for triangle in self._weapon.triangles
            for v in triangle.vertices
        ]
        names = {node.index: node.name for node in self.merged_nodes()}
        by_side: dict[str, list[Vector3]] = defaultdict(list)
        for triangle in placed.triangles:
            for vertex in triangle.vertices:
                side = merged_side.get(vertex.bone)
                if side in ("L", "R") and "Forearm" not in names.get(vertex.bone, ""):
                    by_side[side].append(gripped(vertex))

        seat: dict[str, Vector3] = {}
        for side, points in by_side.items():
            # Iterate closest-point seating: one median slide lands the grip only
            # partway when the rig parks the wrist far off, so re-measure from the
            # slid position and add, until the fingers settle on the gun.
            total = _ZERO
            for _ in range(_SEAT_ITERATIONS):
                disp = sorted(
                    (
                        (q.x - sx, q.y - sy, q.z - sz, dist)
                        for p in points
                        for sx, sy, sz in ((p.x + total.x, p.y + total.y, p.z + total.z),)
                        for q, dist in (_nearest_point(Vector3(sx, sy, sz), gun_pts),)
                    ),
                    key=lambda e: e[3],
                )
                contact = disp[: max(1, len(disp) // 3)]
                step = Vector3(
                    median([e[0] for e in contact]),
                    median([e[1] for e in contact]),
                    median([e[2] for e in contact]),
                )
                total = Vector3(total.x + step.x, total.y + step.y, total.z + step.z)
                if step.x * step.x + step.y * step.y + step.z * step.z < _SEAT_TOLERANCE**2:
                    break
            seat[side] = total
        return seat

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
        # the weapon bones through the animation). ``grip_slide`` moves the gun the
        # same way, but the wrapping fingers follow it (see ``_frame_poses``) so the
        # grip slides forward as a whole while the thumb clears the moving body.
        position = Vector3(
            vertex.position.x + self._weapon_offset.x + self._grip_slide.x,
            vertex.position.y + self._weapon_offset.y + self._grip_slide.y,
            vertex.position.z + self._weapon_offset.z + self._grip_slide.z,
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
                if chain.curl:
                    self._apply_curl(chain, finger_local, world_cache)
                # Wrapping fingers follow the gun as it slides forward, so they stay
                # on the grip; the thumb does not, so the moving body clears it.
                shift = _ZERO
                weapon_tip = weapon_world.get(chain.weapon_tip)
                sliding = self._grip_slide.x or self._grip_slide.y or self._grip_slide.z
                if chain.follow and weapon_tip is not None and sliding:
                    shift = weapon_tip.rotate_vector(self._grip_slide)
                self._solve_finger_ik(chain, finger_local, world_cache, weapon_world, shift)

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
    def _apply_curl(
        chain: _FingerChain,
        finger_local: dict[int, Transform],
        world_cache: dict[int, Transform],
    ) -> None:
        """Flex the finger ``chain.curl`` radians deeper than the weapon authored.

        The weapon's own grip is reproduced exactly by the aim, which for the
        trigger finger can leave it draped over the guard rather than tucked in.
        This adds real finger flexion -- curling the fingertip toward the palm in
        the finger's own bend plane, split across the knuckle and middle joint --
        so the finger sinks into the guard. :meth:`_solve_finger_ik` then pulls
        the tip back onto the trigger, so the finger *body* tucks in while the tip
        stays on the grip contact.
        """
        joints = chain.joints
        if len(joints) < 3:
            return
        base = world_cache[chain.base]
        locals_ = [finger_local[i] for i in joints]

        def forward() -> list[Transform]:
            worlds = [base.compose(locals_[0])]
            for k in range(1, len(locals_)):
                worlds.append(worlds[k - 1].compose(locals_[k]))
            return worlds

        worlds = forward()
        points = [w.translation for w in worlds]
        normal = _cross(_subtract(points[1], points[0]), _subtract(points[2], points[1]))
        if normal.length() < 1e-6:
            return
        normal = _normalize(normal)
        # Flexion sign: the rotation that curls the fingertip toward the palm
        # (the hand base) -- i.e. closes the finger, the way a grip tightens.
        arm = _subtract(points[2], points[0])
        plus = _add(points[0], Transform(axis_angle(normal, 0.05), _ZERO).rotate_vector(arm))
        sign = (
            1.0
            if _subtract(plus, base.translation).length()
            < _subtract(points[2], base.translation).length()
            else -1.0
        )
        # Split the flexion knuckle-heavy so the whole finger sinks into the guard.
        for k, weight in ((0, 0.6), (1, 0.4)):
            parent_rot = base.rotation if k == 0 else worlds[k - 1].rotation
            rot = axis_angle(normal, sign * chain.curl * weight)
            new_world_rot = mat3_multiply(rot, worlds[k].rotation)
            local_rot = mat3_multiply(mat3_transpose(parent_rot), new_world_rot)
            locals_[k] = Transform(local_rot, locals_[k].translation)
            worlds = forward()

        for k, index in enumerate(joints):
            finger_local[index] = locals_[k]
            world_cache[index] = worlds[k]

    @staticmethod
    def _solve_finger_ik(
        chain: _FingerChain,
        finger_local: dict[int, Transform],
        world_cache: dict[int, Transform],
        weapon_world: dict[int, Transform],
        goal_shift: Vector3 = _ZERO,
        iterations: int = 16,
        tolerance: float = 0.02,
        max_curl: float = 2.0,
    ) -> None:
        """Fold the finger onto the grip with FABRIK, then reorient the bones.

        A reference finger is longer than the weapon finger it replaces, and the
        grip contact (the weapon fingertip) is often *closer* to the knuckle than
        the finger is long -- e.g. our index reaches ~2.3 units but the trigger
        sits ~1.4 from the knuckle. Such a target can only be met by folding the
        finger back on itself. Direction-only CCD does not fold (the finger is
        already aimed correctly, just too long, so it juts straight past the
        trigger guard). FABRIK solves reach-with-fold directly: it drags the joint
        chain onto the target while preserving bone lengths, distributing the
        fold across the knuckles so the finger tucks into the guard.

        The FABRIK joint positions are converted back to bone rotations by
        swinging each bone from its aim direction onto the solved segment
        direction (roll is kept from the aim). Each bone's swing is capped at
        ``max_curl`` radians so it cannot fold implausibly far.
        """
        target = weapon_world.get(chain.weapon_tip)
        if target is None or len(chain.joints) < 2:
            return
        goal = _add(target.translation, goal_shift)
        base = world_cache[chain.base]
        if chain.retract:
            # Pull the contact back toward the hand so the longer mesh, not the
            # bone tip, meets the grip -- clearing the weapon it would poke through.
            back = _subtract(base.translation, goal)
            length = back.length()
            if length > 1e-6:
                goal = _add(goal, _scale(back, chain.retract / length))
        locals_ = [finger_local[i] for i in chain.joints]
        aim_rotation = [local.rotation for local in locals_]

        def forward() -> list[Transform]:
            worlds = [base.compose(locals_[0])]
            for k in range(1, len(locals_)):
                worlds.append(worlds[k - 1].compose(locals_[k]))
            return worlds

        worlds = forward()
        points = [w.translation for w in worlds]
        root = points[0]
        lengths = [_subtract(points[i + 1], points[i]).length() for i in range(len(points) - 1)]
        reach = sum(lengths)

        # --- FABRIK: solve joint positions (seeded from the aim pose) ---
        solved = list(points)
        if _subtract(goal, root).length() >= reach:
            direction = _normalize(_subtract(goal, root))
            for i in range(1, len(solved)):
                solved[i] = _add(solved[i - 1], _scale(direction, lengths[i - 1]))
        else:
            for _ in range(iterations):
                solved[-1] = goal
                for i in range(len(solved) - 2, -1, -1):
                    d = _normalize(_subtract(solved[i], solved[i + 1]))
                    solved[i] = _add(solved[i + 1], _scale(d, lengths[i]))
                solved[0] = root
                for i in range(1, len(solved)):
                    d = _normalize(_subtract(solved[i], solved[i - 1]))
                    solved[i] = _add(solved[i - 1], _scale(d, lengths[i - 1]))
                if _subtract(solved[-1], goal).length() < tolerance:
                    break

        # --- reorient each bone from its aim direction onto the solved segment ---
        for i in range(len(locals_) - 1):
            parent_rot = base.rotation if i == 0 else worlds[i - 1].rotation
            cur_dir = _subtract(worlds[i + 1].translation, worlds[i].translation)
            new_dir = _subtract(solved[i + 1], solved[i])
            if cur_dir.length() < 1e-6 or new_dir.length() < 1e-6:
                continue
            swing = rotation_between(cur_dir, new_dir)
            new_world_rot = mat3_multiply(swing, worlds[i].rotation)
            local_rot = mat3_multiply(mat3_transpose(parent_rot), new_world_rot)
            curl = clamp_rotation(
                mat3_multiply(mat3_transpose(aim_rotation[i]), local_rot), max_curl
            )
            locals_[i] = Transform(mat3_multiply(aim_rotation[i], curl), locals_[i].translation)
            worlds = forward()

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


def rigid_weapon_bones(
    weapon: Smd,
    animations: Iterable[Smd],
    *,
    tolerance: float = 0.5,
    min_body_fraction: float = 0.5,
) -> set[int]:
    """Weapon bones that belong to a substantial rigid body of the gun.

    The frame and grip are rigid; sub-parts (a revolver's rounds, a pistol's
    slide, a hammer) swing free during reload or fire, and an akimbo weapon has
    *two* independent bodies that move apart on the draw. Bones are clustered into
    rigid groups -- two bones share a group when the offset between them, in one's
    own frame, stays put across every animation frame -- seeding from the
    largest-mesh bones. Groups carrying at least ``min_body_fraction`` of the
    biggest group's vertices are kept as grip candidates; the tiny sub-part groups
    (a bullet, a slide) are dropped, so a hand can never bind to one and float off.
    """
    counts = Counter(v.bone for triangle in weapon.triangles for v in triangle.vertices)
    if not counts:
        return set()
    names = {node.index: node.name for node in weapon.nodes}
    frames: list[dict[int, Transform]] = []
    for animation in animations:
        index = {node.name: node.index for node in animation.nodes}
        for pose in animation.frames:
            world = world_transforms(animation.nodes, pose)
            frames.append(
                {bone: world[index[names[bone]]] for bone in counts if names[bone] in index}
            )

    def rigid(a: int, b: int) -> bool:
        offsets = [
            f[a].inverse().transform_point(f[b].translation)
            for f in frames
            if a in f and b in f
        ]
        if len(offsets) < 2:
            return True
        spread: float = sum(pvariance([o[axis] for o in offsets]) ** 0.5 for axis in range(3))
        return spread < tolerance

    remaining = set(counts)
    bodies: list[set[int]] = []
    for seed in sorted(counts, key=lambda b: -counts[b]):
        if seed not in remaining:
            continue
        group = {bone for bone in remaining if rigid(seed, bone)}
        bodies.append(group)
        remaining -= group

    biggest = max(sum(counts[bone] for bone in group) for group in bodies)
    return {
        bone
        for group in bodies
        if sum(counts[b] for b in group) >= min_body_fraction * biggest
        for bone in group
    }


def _nearest_point(p: Vector3, points: list[Vector3]) -> tuple[Vector3, float]:
    """The point in ``points`` nearest ``p``, and the distance to it."""
    best = points[0]
    best_d2 = float("inf")
    for q in points:
        d2 = (p.x - q.x) ** 2 + (p.y - q.y) ** 2 + (p.z - q.z) ** 2
        if d2 < best_d2:
            best_d2, best = d2, q
    return best, best_d2**0.5


def _subtract(a: Vector3, b: Vector3) -> Vector3:
    return Vector3(a.x - b.x, a.y - b.y, a.z - b.z)


def _add(a: Vector3, b: Vector3) -> Vector3:
    return Vector3(a.x + b.x, a.y + b.y, a.z + b.z)


def _cross(a: Vector3, b: Vector3) -> Vector3:
    return Vector3(a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x)


def _scale(v: Vector3, s: float) -> Vector3:
    return Vector3(v.x * s, v.y * s, v.z * s)


def _normalize(v: Vector3) -> Vector3:
    length = v.length()
    return v if length == 0 else Vector3(v.x / length, v.y / length, v.z / length)


def _pose_transform(pose: BonePose) -> Transform:
    return Transform.from_pos_euler(pose.position, pose.rotation)


def _transform_pose(bone: int, transform: Transform) -> BonePose:
    return BonePose(bone=bone, position=transform.translation, rotation=transform.to_euler())


__all__ = ["HandGraft"]
