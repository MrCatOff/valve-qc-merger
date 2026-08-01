"""Bone pooling for merge-view (spec §3.7): beat the 127-bone budget.

Only one weapon draws at a time, so unrelated weapons share bone slots named
``Bone_WPNJ{n}_TYPE1``; canonical hand bones (the shared set) keep their names.

Slot selection, per bone in topological order (prior-art rule):

1. prefer a free slot whose recorded parent key equals the bone's natural
   parent key — costs nothing;
2. else claim ANY free slot whose parent key is available to this model (a
   slot it already claimed, a shared bone, or the root) — the bone is then
   REPARENTED under that parent when the plan is applied (exact, per frame);
3. else grow the pool with a new slot under the natural parent key.

Because bones are processed parents-first, a claimed slot's parent was always
claimed by an earlier bone, which can never be a descendant — so the apply
step's reparents cannot form cycles. The merged node table sees exactly one
parent per slot by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.merge_view.skeleton_ops import rename_bones, reparent_bone

SLOT_FORMAT = "Bone_WPNJ{n}_TYPE1"
_ROOT_KEY = ""  # parent key of root-level slots (under the merged Bip01)


@dataclass
class PoolPlan:
    """The shared slot table and each model's bone -> slot assignment."""

    slot_parent: dict[str, str] = field(default_factory=dict)  # slot -> parent key
    assignments: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.slot_parent)


def _topological(bones: dict[str, str | None]) -> list[str]:
    order: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen or name not in bones:
            return
        parent = bones[name]
        if parent is not None and parent in bones:
            visit(parent)
        seen.add(name)
        order.append(name)

    for name in sorted(bones):
        visit(name)
    return order


def _slot_number(slot: str) -> int:
    return int(slot.split("WPNJ")[1].split("_")[0])


def plan_pool(
    model_bones: dict[str, dict[str, str | None]],
    shared: set[str],
    *,
    allow_reparent: bool = True,
) -> PoolPlan:
    """Assign every non-shared bone of every model to a pooled slot.

    With ``allow_reparent=False`` only exact parent-key slots are reused
    (rule 1) and the pool otherwise grows: more slots, but zero reparents —
    which keeps each sequence's animation stream at its original size
    (studiomdl's per-sequence anim data is capped at 64K by the u16 channel
    offsets, and a reparented bone's per-frame-varying locals defeat the
    constant-channel compression).
    """
    plan = PoolPlan()
    counter = 0

    def new_slot(parent_key: str) -> str:
        nonlocal counter
        counter += 1
        slot = SLOT_FORMAT.format(n=counter)
        plan.slot_parent[slot] = parent_key
        return slot

    ordered_models = sorted(
        model_bones.items(),
        key=lambda item: (-sum(1 for b in item[1] if b not in shared), item[0]),
    )
    for model_name, bones in ordered_models:
        assignment: dict[str, str] = {}
        used: set[str] = set()
        model_shared = shared & set(bones)
        for bone in _topological(bones):
            if bone in shared:
                continue
            parent = bones.get(bone)
            if parent is None:
                natural_key = _ROOT_KEY
            elif parent in shared:
                natural_key = parent
            else:
                natural_key = assignment[parent]
            free = sorted(
                (slot for slot in plan.slot_parent if slot not in used),
                key=_slot_number,
            )
            # 1. exact parent-key match: no reparent needed on apply.
            slot = next(
                (s for s in free if plan.slot_parent[s] == natural_key), None
            )
            if slot is None and allow_reparent:
                # 2. any free slot whose parent key this model can satisfy.
                claimed = set(assignment.values())
                available = claimed | model_shared | {_ROOT_KEY}
                slot = next(
                    (s for s in free if plan.slot_parent[s] in available), None
                )
            if slot is None:
                slot = new_slot(natural_key)
            assignment[bone] = slot
            used.add(slot)
        plan.assignments[model_name] = assignment
    return plan


def apply_pool(
    model: ModelInput,
    assignment: dict[str, str],
    slot_parent: dict[str, str],
    *,
    root: str = "Bip01",
) -> list[str]:
    """Rename a model's bones to their slots and reparent where keys differ.

    Returns the list of reparented slots. Renames apply to every SMD and the
    QC's bone references; reparents are per-frame exact (skeleton_ops).
    """
    slot_of = dict(assignment)
    reparented: list[str] = []
    for old, new in sorted(slot_of.items()):
        model.qc_text = model.qc_text.replace(f'"{old}"', f'"{new}"')

    for smd in {**model.meshes, **model.anims}.values():
        rename_bones(smd, slot_of)
        name_parent = {
            n.name: next((p.name for p in smd.nodes if p.index == n.parent), None)
            for n in smd.nodes
        }
        present = set(name_parent)
        for bone in sorted(slot_of, key=lambda b: _slot_number(slot_of[b])):
            slot = slot_of[bone]
            if slot not in present:
                continue
            expected = slot_parent[slot] or root
            if name_parent.get(slot) != expected and expected in present:
                reparent_bone(smd, slot, expected)
                name_parent = {
                    n.name: next(
                        (p.name for p in smd.nodes if p.index == n.parent), None)
                    for n in smd.nodes
                }
                if slot not in reparented:
                    reparented.append(slot)
    return reparented


__all__ = ["PoolPlan", "apply_pool", "plan_pool", "SLOT_FORMAT"]
