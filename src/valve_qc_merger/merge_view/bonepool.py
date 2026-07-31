"""Bone pooling for merge-view (spec §3.7): beat the 127-bone budget.

Only one weapon draws at a time, so unrelated weapons can share bone slots.
Canonical hand bones (the shared set) keep their names; every other bone maps
to a pooled slot ``Bone_WPNJ{n}_TYPE1``. The invariant that makes the merged
node table well-formed is enforced *by construction*: a slot is created under
exactly one parent key (either another slot or a shared bone name), and a
model may only claim a free slot whose parent key matches its bone's parent —
so no slot ever needs two different parents.

Applying a plan is pure renaming: because slot parentage mirrors the model's
own parentage, hierarchy and animation data survive untouched (no reparenting,
trivially pose-exact).
"""

from __future__ import annotations

from dataclasses import dataclass, field

SLOT_FORMAT = "Bone_WPNJ{n}_TYPE1"


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


def plan_pool(
    model_bones: dict[str, dict[str, str | None]],
    shared: set[str],
) -> PoolPlan:
    """Assign every non-shared bone of every model to a pooled slot.

    ``model_bones`` maps model name to its ``{bone: parent}`` table (from the
    canonicalised reference mesh). The largest model is processed first so it
    shapes the pool; slots are reused across models whenever the parent key
    matches, and the pool grows only when no compatible slot is free.
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
        for bone in _topological(bones):
            if bone in shared:
                continue
            parent = bones.get(bone)
            if parent is None:
                parent_key = ""  # a true root pools under the merged root
            elif parent in shared:
                parent_key = parent
            else:
                parent_key = assignment.get(parent, parent)
            candidate = next(
                (slot for slot, slot_parent in sorted(plan.slot_parent.items())
                 if slot_parent == parent_key and slot not in used),
                None,
            )
            slot = candidate or new_slot(parent_key)
            assignment[bone] = slot
            used.add(slot)
        plan.assignments[model_name] = assignment
    return plan


__all__ = ["PoolPlan", "plan_pool", "SLOT_FORMAT"]
