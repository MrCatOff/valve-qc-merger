"""Weapon-bone analysis and chain collapse for merge-p.

A decompiled p_ model carries the shared ``Bip01`` chain plus a small
non-shared subtree per held object, usually ``flash -> weapon`` hanging off a
hand. The engine bone-merges the ``Bip01`` bones by name at runtime, so only
the weapon bones are per-model state — and to respect the 127-bone budget each
held object must cost exactly ONE bone. Every non-shared subtree is collapsed
onto its single vertex-bearing bone (verts rebound, transforms folded exactly
per frame), reparented directly under its anchoring ``Bip01`` bone and renamed
to a name unique across the corpus (the model's directory name).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.merge_view.skeleton_ops import (
    rebind_vertices,
    remove_bones,
    rename_bones,
    reparent_bone,
)
from valve_qc_merger.models.smd import Smd

SHARED_PREFIX = "bip01"


class PlayerAnalyzeError(RuntimeError):
    """A p_ model whose skeleton cannot be reduced to one bone per weapon."""


def is_shared(name: str) -> bool:
    """Whether a bone belongs to the player skeleton (bone-merged by name)."""
    return name.lower().startswith(SHARED_PREFIX)


@dataclass
class WeaponBone:
    """One held object's surviving bone after the collapse."""

    final: str  # merged bone name (unique: derived from the model dir name)
    original: str  # the surviving source bone's original name
    anchor: str  # shared Bip01 bone it hangs from (usually a hand)
    removed: tuple[str, ...] = ()  # chain bones folded away (flash helpers)


@dataclass
class PlayerPlan:
    """Result of collapsing one model: its weapon bones and diagnostics."""

    model: str
    bones: list[WeaponBone] = field(default_factory=list)
    shared: list[str] = field(default_factory=list)  # Bip01 bones this model uses
    warnings: list[str] = field(default_factory=list)

    @property
    def bone_map(self) -> dict[str, str]:
        """original bone name -> merged bone name (shared bones map to themselves)."""
        mapping = {name: name for name in self.shared}
        mapping.update({b.original: b.final for b in self.bones})
        return mapping


def _subtrees(smd: Smd) -> list[list[int]]:
    """Non-shared subtrees of the node table, each rooted just under a shared bone."""
    name_of = {n.index: n.name for n in smd.nodes}
    children: dict[int, list[int]] = {}
    for node in smd.nodes:
        children.setdefault(node.parent, []).append(node.index)
    roots = [
        n.index for n in smd.nodes
        if not is_shared(n.name) and (n.parent < 0 or is_shared(name_of[n.parent]))
    ]
    trees: list[list[int]] = []
    for root in roots:
        tree, queue = [], [root]
        while queue:
            index = queue.pop(0)
            tree.append(index)
            queue.extend(children.get(index, []))
        trees.append(tree)
    return trees


def _is_left(anchor: str) -> bool:
    return " l " in f" {anchor.lower()} "


def _final_names(model_name: str, anchors: list[str]) -> list[str]:
    """Unique merged bone names; in a dual-wield pair the right-hand bone owns
    the base name and the left-hand one gets an ``_L`` suffix (whatever order
    the subtrees appear in the node table)."""
    if len(anchors) <= 1:
        return [model_name] * len(anchors)
    if len(anchors) == 2 and _is_left(anchors[0]) != _is_left(anchors[1]):
        return [
            f"{model_name}_L" if _is_left(anchor) else model_name
            for anchor in anchors
        ]
    return [model_name] + [
        f"{model_name}_{position + 1}" for position in range(1, len(anchors))
    ]


def collapse_weapon_bones(model: ModelInput) -> PlayerPlan:
    """Reduce every held object to one bone, applied to ALL of the model's SMDs.

    Mutates the model's meshes and anims in place: vertices of collapsed chain
    bones are rebound to the survivor (positions are model-space, so nothing
    moves), the survivor is reparented under the anchor with a per-frame-exact
    re-solve, the helper bones are removed with their transforms folded in, and
    the survivor is renamed to a corpus-unique name.
    """
    fullest = max(model.meshes.values(), key=lambda m: len(m.nodes))
    name_of = {n.index: n.name for n in fullest.nodes}
    plan = PlayerPlan(model=model.name)
    plan.shared = [n.name for n in fullest.nodes if is_shared(n.name)]
    if not plan.shared:
        raise PlayerAnalyzeError(
            f"model {model.name!r}: no {SHARED_PREFIX!r}* player bones at all"
        )

    used: set[str] = set()
    for mesh in model.meshes.values():
        mesh_names = {n.index: n.name for n in mesh.nodes}
        used.update(mesh_names[v.bone] for t in mesh.triangles for v in t.vertices)

    groups: list[tuple[str, str, set[str]]] = []  # (survivor, anchor, removed)
    for tree in _subtrees(fullest):
        tree_names = [name_of[i] for i in tree]
        root = next(n for n in fullest.nodes if n.index == tree[0])
        if root.parent < 0:
            raise PlayerAnalyzeError(
                f"model {model.name!r}: non-player bone {root.name!r} is a root "
                "(cannot anchor it to the Bip01 chain)"
            )
        anchor = name_of[root.parent]
        if not anchor.lower().endswith(" hand"):
            plan.warnings.append(
                f"weapon bones anchor to {anchor!r} (not a hand bone)"
            )
        bearing = [name for name in tree_names if name in used]
        if not bearing:
            plan.warnings.append(
                f"bones {tree_names} carry no vertices; removed entirely"
            )
            for smd in {**model.meshes, **model.anims}.values():
                present = {n.name for n in smd.nodes}
                remove_bones(smd, set(tree_names) & present)
            continue
        # Deepest vertex-bearing bone survives (chains are parent-first).
        survivor = bearing[-1]
        groups.append((survivor, anchor, set(tree_names) - {survivor}))

    anchors = [anchor for _s, anchor, _r in groups]
    finals = _final_names(model.name, anchors)
    for (survivor, anchor, removed), final in zip(groups, finals, strict=True):
        for smd in {**model.meshes, **model.anims}.values():
            present = {n.name for n in smd.nodes}
            if survivor not in present:
                continue
            for gone in sorted(removed & present):
                rebind_vertices(smd, gone, survivor)
            parent_of = {
                n.name: next((p.name for p in smd.nodes if p.index == n.parent), None)
                for n in smd.nodes
            }
            if parent_of.get(survivor) != anchor:
                reparent_bone(smd, survivor, anchor)
            remove_bones(smd, removed & present)
            rename_bones(smd, {survivor: final})
        plan.bones.append(WeaponBone(
            final=final, original=survivor, anchor=anchor,
            removed=tuple(sorted(removed)),
        ))
    return plan


__all__ = [
    "PlayerAnalyzeError",
    "PlayerPlan",
    "WeaponBone",
    "collapse_weapon_bones",
    "is_shared",
]
