"""Shared attachment slots for merge-v (muzzle flash, shell eject, ...).

GoldSrc caps a model at 4 attachments (``cl_entity_t.attachment[4]``) and an
attachment is a fixed offset from ONE bone, but every merged weapon carries
its own muzzle on its own (pooled) bone. Keeping only the first model's
``$attachment`` lines put every other weapon's flash at that weapon's muzzle
-- or nowhere, if the bone is hidden.

Fix: up to 4 SHARED slot bones ``attachment0..3`` (children of ``Bip01``),
one ``$attachment N "attachmentN" 0 0 0`` each. Every SMD of every model
animates slot N to that model's own attachment N (its bone's world pose
composed with the QC offset), per frame. Each weapon's sequences therefore
drive the shared slots to its own muzzle/shell points; a weapon lacking slot
N parks it at ``Bip01``.

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
from valve_qc_merger.merge_view.skeleton_ops import fk_worlds
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd, Triangle
from valve_qc_merger.transform import Transform

ATTACHMENT_LIMIT = 4  # cl_entity_t.attachment[4]
SLOT_BONE = "attachment{}"
_ROOT = "Bip01"

_ATTACH_RE = re.compile(
    r'^\s*\$attachment\s+(?P<idx>\d+)\s+(?:"(?P<qbone>[^"]+)"|(?P<bone>\S+))\s+'
    r"(?P<x>\S+)\s+(?P<y>\S+)\s+(?P<z>\S+)",
    re.MULTILINE | re.IGNORECASE,
)


@dataclass
class SharedAttachments:
    """What the shared-slot pass did."""

    slots: int = 0
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


def _add_slot_bones(
    smd: Smd,
    slots: int,
    attachments: dict[int, tuple[str, Vector3]],
) -> list[int]:
    """Append the slot bones to one SMD, posed per frame; returns their indices."""
    index_of = {n.name: n.index for n in smd.nodes}
    root = index_of.get(_ROOT, -1)
    first = max((n.index for n in smd.nodes), default=-1) + 1
    indices = list(range(first, first + slots))
    zero = Vector3(0.0, 0.0, 0.0)
    frames: list[Frame] = []
    for frame in smd.frames:
        worlds = fk_worlds(smd, frame) if attachments else {}
        root_inv = (worlds[root].inverse() if root >= 0 and root in worlds
                    else None)
        poses = list(frame.poses)
        for slot, index in enumerate(indices):
            source = attachments.get(slot)
            bone = index_of.get(source[0]) if source is not None else None
            if source is None or bone is None or bone not in worlds:
                poses.append(BonePose(index, zero, zero))
                continue
            world = worlds[bone].compose(Transform(translation=source[1]))
            local = root_inv.compose(world) if root_inv is not None else world
            poses.append(BonePose(index, local.translation, local.to_euler()))
        frames.append(Frame(frame.time, tuple(poses)))
    smd.frames = frames
    # Appended only now: fk_worlds above must not see the pose-less slots.
    smd.nodes = [*smd.nodes, *(Node(i, SLOT_BONE.format(s), root)
                               for s, i in enumerate(indices))]
    return indices


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

    indices: list[int] = []
    done: set[int] = set()
    for model in models:
        for smd in {**model.meshes, **model.anims}.values():
            if id(smd) in done:
                continue
            done.add(id(smd))
            got = _add_slot_bones(smd, slots, parsed[model.name])
            if smd is anchor:
                indices = got
    if not indices:
        raise ValueError("anchor mesh is not one of the models' meshes")
    if not anchor.triangles:
        raise ValueError("anchor mesh has no triangles to borrow a vertex from")
    seed = anchor.triangles[0]
    vertex = seed.vertices[0]
    for index in indices:
        pinned = dataclasses.replace(vertex, bone=index)
        anchor.triangles.append(
            Triangle(seed.material, (pinned, pinned, pinned))
        )
    result.qc_lines = [
        f'$attachment {slot} "{SLOT_BONE.format(slot)}" 0 0 0'
        for slot in range(slots)
    ]
    return result


__all__ = [
    "ATTACHMENT_LIMIT",
    "SharedAttachments",
    "parse_model_attachments",
    "share_attachments",
]
