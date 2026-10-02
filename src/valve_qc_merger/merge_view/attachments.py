"""Shared attachment slots for merge-v (muzzle flash, shell eject, ...).

GoldSrc caps a model at 4 attachments (``cl_entity_t.attachment[4]``) and an
attachment is a fixed offset from ONE bone, but every merged weapon carries
its own muzzle on its own (pooled) bone. Keeping only the first model's
``$attachment`` lines put every other weapon's flash at that weapon's muzzle
-- or nowhere, if the bone is hidden.

Fix: up to 4 SHARED slot bones ``attachment0..3``, one
``$attachment N "attachmentN" 0 0 0`` each. Every SMD of every model
animates slot N to that model's own attachment N (its bone's world pose
composed with the QC offset), per frame. Each weapon's sequences therefore
drive the shared slots to its own muzzle/shell points; a weapon lacking slot
N parks it at the slot's parent. Each slot hangs off the wrist its points
stay closest to (``Bip01`` without wrists): relative to the gripping hand a
muzzle barely moves, so its channels are near-constant and RLE-cheap.

A weapon may park a slot far off-map (CSO: ``$attachment 1 ... -1000000``
hides the shell eject). studiomdl quantises each bone channel with ONE scale
over every sequence, so such a slot gets a carrier ``attachmentN_base``: the
carrier takes the near poses, the leaf ``attachmentN`` (child) only the far
ones, and neither range pollutes the other's precision.

studiomdl drops vertex-less bones from the compiled table (and an attachment
naming a dropped bone is a hard error; appended vertex-less leaves also
corrupt the reindex), so each slot bone gets one zero-area anchor triangle --
an existing vertex of one weapon mesh, tripled -- which keeps it alive
without drawing anything or moving any vertex position.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field

from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd, Triangle
from valve_qc_merger.transform import Transform

ATTACHMENT_LIMIT = 4  # cl_entity_t.attachment[4]
SLOT_BONE = "attachment{}"
_ROOT = "Bip01"
# Slot poses beyond this (units from Bip01, any axis) are "parked off-map"
# (CSO hides unused shell ejects at -1000000) and go to a separate bone.
FAR_LIMIT = 1024.0
_HAND_RE = re.compile(r"(?i)(?:^|[ ._])(?:[lr][ ._])?hand(?:[ ._][lr])?$")

_ATTACH_RE = re.compile(
    r'^\s*\$attachment\s+(?P<idx>\d+)\s+(?:"(?P<qbone>[^"]+)"|(?P<bone>\S+))\s+'
    r"(?P<x>\S+)\s+(?P<y>\S+)\s+(?P<z>\S+)",
    re.MULTILINE | re.IGNORECASE,
)


@dataclass
class SharedAttachments:
    """What the shared-slot pass did."""

    slots: int = 0
    bones: int = 0
    parents: dict[int, str] = field(default_factory=dict)
    qc_lines: list[str] = field(default_factory=list)
    per_model: dict[str, dict[int, str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def parse_model_attachments(
    qc_text: str,
) -> tuple[dict[int, tuple[str, Vector3]], list[str]]:
    """``$attachment`` lines as index -> (bone, offset); first per index wins.

    Returns the parsed map plus the raw lines that cannot be shared
    (index >= 4, unparsable offset, duplicate index).
    """
    out: dict[int, tuple[str, Vector3]] = {}
    rejected: list[str] = []
    for m in _ATTACH_RE.finditer(qc_text):
        idx = int(m.group("idx"))
        try:
            offset = Vector3(float(m.group("x")), float(m.group("y")),
                             float(m.group("z")))
        except ValueError:
            rejected.append(m.group(0).strip())
            continue
        if idx >= ATTACHMENT_LIMIT or idx in out:
            rejected.append(m.group(0).strip())
            continue
        out[idx] = (m.group("qbone") or m.group("bone"), offset)
    return out, rejected


def _hand_bones(names: list[str]) -> list[str]:
    """The wrist bones of the canonical hands (``Hand.L``,
    ``ValveBiped.Bip01_R_Hand``, ``Bip01 L Hand``; not ``TweakHand.L``)."""
    return [n for n in names if _HAND_RE.search(n)]


def _slot_worlds(
    smd: Smd, slots: int, attachments: dict[int, tuple[str, Vector3]],
    anchors: list[str],
) -> list[tuple[dict[str, Transform], list[Transform | None]]]:
    """Per frame: world poses of ``anchors`` (+ ``Bip01``) and of every slot
    (``None`` where this model has no such slot or the bone is missing)."""
    index_of = {n.name: n.index for n in smd.nodes}
    sources = {slot: (index_of.get(bone), offset)
               for slot, (bone, offset) in attachments.items() if slot < slots}
    if not any(bone is not None for bone, _o in sources.values()):
        return [({}, [None] * slots) for _frame in smd.frames]
    wanted = [n for n in {_ROOT, *anchors} if n in index_of]
    parent_of = {n.index: n.parent for n in smd.nodes}
    out: list[tuple[dict[str, Transform], list[Transform | None]]] = []
    for frame in smd.frames:
        # FK of just the chains we read (full-skeleton FK per frame was
        # ~90s on the pistol corpus).
        local = {p.bone: p for p in frame.poses}
        worlds: dict[int, Transform] = {}

        def world(index: int, local: dict[int, BonePose] = local,
                  worlds: dict[int, Transform] = worlds) -> Transform:
            cached = worlds.get(index)
            if cached is None:
                pose = local[index]
                xf = Transform.from_pos_euler(pose.position, pose.rotation)
                parent = parent_of[index]
                cached = xf if parent < 0 else world(parent, local, worlds).compose(xf)
                worlds[index] = cached
            return cached

        row: list[Transform | None] = []
        for slot in range(slots):
            bone, offset = sources.get(slot, (None, None))
            if bone is None or bone not in local:
                row.append(None)
                continue
            row.append(world(bone).compose(Transform(translation=offset)))
        out.append(({n: world(index_of[n]) for n in wanted}, row))
    return out


def _relative(parent: Transform | None, world: Transform | None) -> Transform | None:
    if world is None:
        return None
    return parent.inverse().compose(world) if parent is not None else world


def _slot_locals(
    smd: Smd, slots: int, attachments: dict[int, tuple[str, Vector3]],
) -> list[list[Transform | None]]:
    """Per frame, per slot: the attachment pose relative to ``Bip01``."""
    return [[_relative(anchors.get(_ROOT), w) for w in row]
            for anchors, row in _slot_worlds(smd, slots, attachments, [])]


def _is_far(local: Transform | None) -> bool:
    if local is None:
        return False
    t = local.translation
    return max(abs(t.x), abs(t.y), abs(t.z)) > FAR_LIMIT


_FAR_CACHE: dict[int, tuple[ModelInput, int, set[int]]] = {}


def _model_far_slots(model: ModelInput, slots: int) -> set[int]:
    """Slots this model parks beyond ``FAR_LIMIT`` in any frame (cached:
    split planning asks for the same model many times)."""
    cached = _FAR_CACHE.get(id(model))
    if cached is not None and cached[0] is model and cached[1] == slots:
        return cached[2]
    atts = parse_model_attachments(model.qc_text)[0]
    far: set[int] = set()
    for smd in model.anims.values():
        for row in _slot_locals(smd, slots, atts):
            far.update(slot for slot, local in enumerate(row) if _is_far(local))
    _FAR_CACHE[id(model)] = (model, slots, far)
    return far


def attachment_slots(models: list[ModelInput]) -> int:
    """Bones a merge of ``models`` will append for attachments (reserve them
    in the 127-bone budget before pooling): one per slot, plus a far carrier
    for every slot some weapon parks far away."""
    used = {slot for model in models
            for slot in parse_model_attachments(model.qc_text)[0]}
    if not used:
        return 0
    slots = max(used) + 1
    far: set[int] = set()
    for model in models:
        far |= _model_far_slots(model, slots)
    return slots + len(far)


def share_attachments(
    models: list[ModelInput], anchor: Smd,
) -> SharedAttachments:
    """Drive shared slot bones from each model's own ``$attachment`` lines.

    Mutates every mesh/sequence SMD of every model (they must already share
    one node table, i.e. run after ``unify_skeletons``) and ``anchor`` (a
    mesh SMD of the merged model, receiving the zero-area keep-alive
    triangles). Returns the QC lines to emit.
    """
    result = SharedAttachments()
    parsed: dict[str, dict[int, tuple[str, Vector3]]] = {}
    for model in models:
        atts, rejected = parse_model_attachments(model.qc_text)
        bones = {n.name for n in next(iter(model.meshes.values())).nodes}
        for slot, (bone, _offset) in sorted(atts.items()):
            if bone not in bones:
                result.warnings.append(
                    f"{model.name}: attachment {slot} bone {bone!r} not in the "
                    "merged skeleton; slot parked at Bip01 for this weapon"
                )
        for line in rejected:
            result.warnings.append(
                f"{model.name}: {line!r} cannot be shared (GoldSrc keeps "
                f"{ATTACHMENT_LIMIT} attachments, one per index)"
            )
        parsed[model.name] = atts
        result.per_model[model.name] = {
            slot: bone for slot, (bone, _o) in sorted(atts.items())
        }
    used = {slot for atts in parsed.values() for slot in atts}
    if not used:
        return result
    # Contiguous 0..max: studiomdl numbers attachments by QC order, not by
    # the index token, so a gap would shift every later slot.
    slots = max(used) + 1
    result.slots = slots

    # Pass 1: every SMD's slot world poses, plus the candidate parents.
    table = [n.name for n in anchor.nodes]
    hands = _hand_bones(table)
    smds: list[Smd] = []
    data: list[list[tuple[dict[str, Transform], list[Transform | None]]]] = []
    seen: set[int] = set()
    for model in models:
        for smd in {**model.meshes, **model.anims}.values():
            if id(smd) in seen:
                continue
            seen.add(id(smd))
            smds.append(smd)
            data.append(_slot_worlds(smd, slots, parsed[model.name], hands))
    if not any(smd is anchor for smd in smds):
        raise ValueError("anchor mesh is not one of the models' meshes")
    if not anchor.triangles:
        raise ValueError("anchor mesh has no triangles to borrow a vertex from")

    # Off-map slots (judged from Bip01, as the budget reserve does).
    far = sorted({
        slot for rows in data for anchors_w, row in rows
        for slot, world in enumerate(row)
        if _is_far(_relative(anchors_w.get(_ROOT), world))
    })

    # Each slot hangs off the wrist its points stay closest to over every
    # frame of every weapon (Elite: left muzzle/shell -> Hand.L, right ->
    # Hand.R): relative to the gripping hand a muzzle barely moves, so the
    # channels are near-constant -- tiny quantisation steps and RLE-cheap
    # streams under studiomdl's 64K-per-sequence cap. Bip01 if no wrists.
    parent_of: dict[int, str | None] = {}
    for slot in range(slots):
        best, best_cost = None, None
        for hand in hands:
            cost = 0.0
            for rows in data:
                for anchors_w, row in rows:
                    world = row[slot]
                    if world is None or hand not in anchors_w or _is_far(
                            _relative(anchors_w.get(_ROOT), world)):
                        continue
                    d = world.translation
                    h = anchors_w[hand].translation
                    cost += ((d.x - h.x) ** 2 + (d.y - h.y) ** 2
                             + (d.z - h.z) ** 2) ** 0.5
            if best_cost is None or cost < best_cost:
                best, best_cost = hand, cost
        parent_of[slot] = best
    result.parents = {slot: parent or _ROOT for slot, parent in parent_of.items()}

    # Pass 2: append the bones. Node layout (identical in every SMD): per
    # slot, [attachmentN_base ->] attachmentN, parents before children.
    layout: list[tuple[str, str | None]] = []
    for slot in range(slots):
        if slot in far:
            layout.append((SLOT_BONE.format(slot) + "_base", None))
            layout.append((SLOT_BONE.format(slot), SLOT_BONE.format(slot) + "_base"))
        else:
            layout.append((SLOT_BONE.format(slot), None))
    result.bones = len(layout)
    # Top-level bone of each slot chain -> the wrist (or Bip01) it hangs off.
    slot_parent = {name: parent_of[int(name[len("attachment"):].split("_")[0])]
                   or _ROOT for name, parent in layout if parent is None}
    zero = Vector3(0.0, 0.0, 0.0)
    for smd, rows in zip(smds, data, strict=True):
        index_of = {n.name: n.index for n in smd.nodes}
        root = index_of.get(_ROOT, -1)
        first = max((n.index for n in smd.nodes), default=-1) + 1
        new_index = {name: first + i for i, (name, _p) in enumerate(layout)}
        frames: list[Frame] = []
        for frame, (anchors_w, row) in zip(smd.frames, rows, strict=True):
            extra: list[BonePose] = []
            for slot, world in enumerate(row):
                name = SLOT_BONE.format(slot)
                parent = parent_of[slot] or _ROOT
                local = _relative(anchors_w.get(parent), world)
                if slot in far:
                    # Carrier holds near poses, the leaf holds far ones: each
                    # channel's quantisation scale (studiomdl sizes it per
                    # bone over ALL sequences) then only ever spans its own
                    # range, so one weapon's off-map parking (-1e6) can't
                    # coarsen every other weapon's muzzle to ~30u steps.
                    near, away = (
                        (None, local)
                        if _is_far(_relative(anchors_w.get(_ROOT), world))
                        else (local, None))
                    extra.append(_pose(new_index[name + "_base"], near, zero))
                    extra.append(_pose(new_index[name], away, zero))
                else:
                    extra.append(_pose(new_index[name], local, zero))
            frames.append(Frame(frame.time, (*frame.poses, *extra)))
        smd.frames = frames
        smd.nodes = [*smd.nodes, *(
            Node(new_index[name], name,
                 new_index[parent] if parent is not None
                 else index_of.get(slot_parent[name], root))
            for name, parent in layout
        )]
        if smd is anchor:
            seed = anchor.triangles[0]
            for slot in range(slots):
                pinned = dataclasses.replace(
                    seed.vertices[0], bone=new_index[SLOT_BONE.format(slot)])
                anchor.triangles.append(
                    Triangle(seed.material, (pinned, pinned, pinned)))
    result.qc_lines = [
        f'$attachment {slot} "{SLOT_BONE.format(slot)}" 0 0 0'
        for slot in range(slots)
    ]
    return result


def _pose(index: int, local: Transform | None, zero: Vector3) -> BonePose:
    if local is None:
        return BonePose(index, zero, zero)
    return BonePose(index, local.translation, local.to_euler())


__all__ = [
    "ATTACHMENT_LIMIT",
    "SharedAttachments",
    "attachment_slots",
    "parse_model_attachments",
    "share_attachments",
]
