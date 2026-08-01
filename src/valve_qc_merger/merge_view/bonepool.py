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

import copy
from collections.abc import Sequence
from dataclasses import dataclass, field

from valve_qc_merger.merge_view.animsize import sequence_sizes
from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.merge_view.skeleton_ops import rename_bones, reparent_bone
from valve_qc_merger.models.smd import Smd

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


def _slot_number(slot: str) -> int:
    return int(slot.split("WPNJ")[1].split("_")[0])


def _widest_first(bones: dict[str, str | None], shared: set[str]) -> list[str]:
    """Weapon bones ordered parents-first, widest subtree first (prior art).

    The bones with the most structure below them claim slots first, so a
    later model's big subtrees land on the pool's big subtrees and most
    assignments need no reshaping at all.
    """
    weapon = {b for b in bones if b not in shared}
    children: dict[str | None, list[str]] = {}
    for bone in weapon:
        parent = bones.get(bone)
        children.setdefault(parent if parent in weapon else None, []).append(bone)
    sizes: dict[str, int] = {}

    def size(bone: str) -> int:
        if bone not in sizes:
            sizes[bone] = 1 + sum(size(c) for c in children.get(bone, []))
        return sizes[bone]

    order: list[str] = []
    queue = sorted(children.get(None, []), key=lambda b: (-size(b), b))
    while queue:
        bone = queue.pop(0)
        order.append(bone)
        queue = sorted(children.get(bone, []), key=lambda b: (-size(b), b)) + queue
    return order


def _pool_subtree_sizes(slot_parent: dict[str, str]) -> dict[str, int]:
    sizes: dict[str, int] = {}
    children: dict[str, list[str]] = {}
    for slot, parent in slot_parent.items():
        children.setdefault(parent, []).append(slot)

    def size(slot: str) -> int:
        if slot not in sizes:
            sizes[slot] = 1 + sum(size(c) for c in children.get(slot, []))
        return sizes[slot]

    for slot in slot_parent:
        size(slot)
    return sizes


def plan_pool(
    model_bones: dict[str, dict[str, str | None]],
    shared: set[str],
    *,
    max_slots: int | None = None,
    allow_reparent: bool = True,
) -> PoolPlan:
    """Assign every non-shared bone of every model to a pooled slot.

    Prior-art rules, largest model first so it shapes the pool. Per bone:
    take a free slot in the IDEAL position (under the slot the bone's own
    parent claimed; widest pool subtree first so structure lines up); while
    the pool is under ``max_slots``, otherwise GROW — spending a slot is
    cheaper than a reshape, which turns constant channels time-varying and
    eats into studiomdl's 64K-per-sequence budget. Only once the pool is
    full does the search widen: a free slot under an ancestor of the ideal
    position, then any free slot whose pool ancestors this model claimed.

    ``allow_reparent=False`` never reshapes at all (rename-only mode: pool
    grows past ``max_slots`` if it must; the caller checks the budget).
    """
    plan = PoolPlan()
    counter = 0

    ordered_models = sorted(
        model_bones.items(),
        key=lambda item: (-sum(1 for b in item[1] if b not in shared), item[0]),
    )
    for model_name, bones in ordered_models:
        assignment: dict[str, str] = {}
        claimed: set[str] = set()
        sizes = _pool_subtree_sizes(plan.slot_parent)
        children: dict[str, list[str]] = {}
        for existing, parent_key in plan.slot_parent.items():
            children.setdefault(parent_key, []).append(existing)

        def free_under(key: str, children: dict[str, list[str]] = children,
                       claimed: set[str] = claimed) -> list[str]:
            return [c for c in children.get(key, []) if c not in claimed]

        for bone in _widest_first(bones, shared):
            parent = bones.get(bone)
            if parent is None:
                ideal = _ROOT_KEY
            elif parent in shared:
                ideal = parent
            else:
                ideal = assignment[parent]

            candidates = free_under(ideal)
            slot: str | None = (
                max(candidates, key=lambda c: (sizes.get(c, 1), -_slot_number(c)))
                if candidates else None
            )

            at_budget = max_slots is not None and plan.size >= max_slots
            if slot is None and allow_reparent and at_budget:
                # Pool is full: widen up the ideal position's ancestor chain.
                node = plan.slot_parent.get(ideal)
                while node is not None:
                    candidates = free_under(node)
                    if candidates:
                        slot = max(candidates,
                                   key=lambda c: (sizes.get(c, 1),
                                                  -_slot_number(c)))
                        break
                    node = plan.slot_parent.get(node)
                if slot is None:
                    # Any free slot whose pool ancestors this model already
                    # claimed — shallowest first (least foreign motion).
                    spare = [
                        s for s in plan.slot_parent
                        if s not in claimed
                        and _ancestors_claimed(plan.slot_parent, s, claimed)
                    ]
                    if spare:
                        slot = min(spare, key=lambda s: (
                            _depth(plan.slot_parent, s), _slot_number(s)))
            if slot is None:
                counter += 1
                slot = SLOT_FORMAT.format(n=counter)
                plan.slot_parent[slot] = ideal
                children.setdefault(ideal, []).append(slot)
                sizes[slot] = 1
            assignment[bone] = slot
            claimed.add(slot)
        plan.assignments[model_name] = assignment
    return plan


def _ancestors_claimed(
    slot_parent: dict[str, str], slot: str, claimed: set[str],
) -> bool:
    cursor = slot_parent.get(slot)
    while cursor is not None and cursor in slot_parent:
        if cursor not in claimed:
            return False
        cursor = slot_parent.get(cursor)
    return True


def _depth(slot_parent: dict[str, str], slot: str) -> int:
    depth = 0
    cursor = slot_parent.get(slot)
    while cursor is not None and cursor in slot_parent:
        depth += 1
        cursor = slot_parent.get(cursor)
    return depth


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
        reparented.extend(_apply_to_smd(smd, slot_of, slot_parent, root))
    return sorted(set(reparented))


def _apply_to_smd(
    smd: Smd, slot_of: dict[str, str], slot_parent: dict[str, str], root: str,
) -> list[str]:
    reparented: list[str] = []
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
            reparented.append(slot)
    return reparented


def preview_pool_sizes(
    pairs: Sequence[tuple[ModelInput, object]],
    plan: PoolPlan,
    *,
    root: str = "Bip01",
) -> dict[tuple[str, str], int]:
    """Sequence anim-stream sizes as if *plan* were applied — mutates nothing.

    Clones each model's sequences (and fullest mesh, for the binds), applies
    the plan's renames/reparents to the clones and runs the byte-exact
    studiomdl size replica. Used to reject a pooling plan whose reparents
    would push a sequence past the 64K cap before anything is written.
    """
    from valve_qc_merger.merge_view.merger import merged_skeleton, unify_skeletons

    fakes: list[ModelInput] = []
    for model, _parts in pairs:
        fullest = copy.deepcopy(
            max(model.meshes.values(), key=lambda m: len(m.nodes))
        )
        clone = ModelInput(
            name=model.name, directory=model.directory,
            qc_path=model.qc_path, qc_text="", bodygroups={}, sequences=[],
            meshes={"_": fullest},
            anims={k: copy.deepcopy(v) for k, v in model.anims.items()},
        )
        slot_of = plan.assignments[model.name]
        for smd in {**clone.meshes, **clone.anims}.values():
            _apply_to_smd(smd, slot_of, plan.slot_parent, root)
        fakes.append(clone)
    # The real merge grafts + re-solves canonical parents into every SMD;
    # run the same unification on the clones so the sizes are what studiomdl
    # will actually compress.
    unify_skeletons(fakes, merged_skeleton(fakes))
    return sequence_sizes(fakes)


__all__ = ["PoolPlan", "apply_pool", "plan_pool", "preview_pool_sizes",
           "SLOT_FORMAT"]
