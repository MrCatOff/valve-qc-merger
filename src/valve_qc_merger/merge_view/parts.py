"""Part splitting for merge-v (spec M5): respect studiomdl's hard budgets.

HLSDK studiomdl stores submodels in fixed arrays of ``MAXSTUDIOMODELS`` (32)
entries with NO bounds checks: a ``$bodygroup`` set exceeding 32 total
submodels silently corrupts the compiler's memory and produces a model whose
triangles detach from its bones (community builds raise vertex limits but keep
the 32-model arrays, so they crash or corrupt exactly the same way). Textures
cap at ``MAXSTUDIOSKINS`` (100) and degrade in tools well before that.

So the merge is split into parts: each part compiles to its own .mdl whose
submodel count (weapon groups + blanks + hand meshes) and texture count
stay inside the budget. Models are packed greedily in input order.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from valve_qc_merger.merge_view.attachments import attachment_slots
from valve_qc_merger.merge_view.bodygroups import ModelParts
from valve_qc_merger.merge_view.bonepool import plan_pool
from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.writers.smd import write_smd_text

# Hard studiomdl array size; exceeding it is memory corruption, not an error.
SUBMODEL_LIMIT = 32
TEXTURE_BUDGET = 80
BONE_BUDGET = 127
# Per-weapon hands (default): parts are submodel-bound (~16 weapons) and a
# looser sequence budget only packs more reparent-heavy models together.
SEQUENCE_BUDGET = 111
# --shared-hands: 31 weapons fit the submodel cap, so sequences bind instead.
# The game picks a viewmodel animation by a BYTE (SendWeaponAnim ->
# WRITE_BYTE): one v_ model addresses 256 sequences; 255 keeps a margin.
SHARED_HANDS_SEQUENCE_BUDGET = 255

Pair = tuple[ModelInput, ModelParts]


@dataclass(frozen=True)
class PartBudget:
    """Per-part ceilings; submodels is a hard compiler limit."""

    submodels: int = SUBMODEL_LIMIT
    textures: int = TEXTURE_BUDGET
    bones: int = BONE_BUDGET
    sequences: int = SEQUENCE_BUDGET


def _texture_keys(model: ModelInput, parts: ModelParts) -> set[tuple[str, str]]:
    """Approximate the staged-texture identity: (material, file content)."""
    stems = [stem for group in parts.weapon_stems for stem in group]
    if parts.hands_stem is not None:
        stems.append(parts.hands_stem)
    keys: set[tuple[str, str]] = set()
    for stem in stems:
        for material in {t.material for t in model.meshes[stem].triangles}:
            wanted = {material.lower(), (material + ".bmp").lower()}
            digest = ""
            for candidate in sorted(model.directory.iterdir()):
                if candidate.is_file() and candidate.name.lower() in wanted:
                    digest = hashlib.md5(candidate.read_bytes()).hexdigest()
                    break
            keys.add((material.lower(), digest))
    return keys


def _sequence_keys(model: ModelInput) -> list[tuple[str, float | None, tuple[str, ...]]]:
    """Dedupe identity of each sequence: (anim bytes, fps, events).

    Matches the merge-time dedupe: recolour variants of one weapon carry
    byte-identical animations, so within a part they compile to ONE
    $sequence that every variant's manifest entries point at.
    """
    keys = []
    for seq_name, anim in model.anims.items():
        meta = next((s for s in model.sequences if s.name == seq_name), None)
        keys.append((
            hashlib.md5(write_smd_text(anim).encode("latin-1")).hexdigest(),
            meta.fps if meta is not None else None,
            meta.events if meta is not None else (),
        ))
    return keys


def _part_counts(
    part: list[Pair],
    textures: dict[str, set[tuple[str, str]]],
    seq_keys: dict[str, list[tuple[str, float | None, tuple[str, ...]]]],
    *,
    shared_hands: bool = False,
) -> tuple[int, int, int]:
    """(submodels, textures, sequences) a part would compile to.

    Hands are one submodel per model (kept per-model so every hands SMD pairs
    with its own weapon's bind; models without hands get a "blank" entry, and
    a blank still occupies one of studiomdl's model slots). With
    ``shared_hands`` the merger emits ONE hands group holding the first
    model's variants (male/female), whatever the part size -- counting a
    hands entry per model capped shared-hands parts at ~15 weapons instead
    of 31.
    """
    groups = [len(parts.weapon_stems) for _, parts in part]
    max_groups = max(groups)
    blanks = max_groups - 1 if max_groups > 1 else 0
    any_hands = any(parts.hands_stem is not None for _, parts in part)
    if shared_hands:
        hands = len(part[0][1].hand_variants)
    else:
        hands = len(part) if any_hands else 0
    submodels = sum(groups) + blanks + hands
    materials: set[tuple[str, str]] = set()
    sequences: set[tuple[str, float | None, tuple[str, ...]]] = set()
    for model, _ in part:
        materials |= textures[model.name]
        sequences.update(seq_keys[model.name])
    return submodels, len(materials), len(sequences)


def split_parts(
    pairs: list[Pair],
    budget: PartBudget | None = None,
    *,
    model_bones: dict[str, dict[str, str | None]] | None = None,
    shared: set[str] | None = None,
    shared_hands: bool = False,
) -> list[list[Pair]]:
    """Greedily pack models into parts that fit the budget.

    When ``model_bones``/``shared`` are given, a part must also fit the bone
    budget under structure-matched pooling (prior-art rules: the pool stays
    near the largest single model's weapon-bone count, so a dozen weapons
    share ~90 slots). The CLI previews each part's pooled sequence sizes and
    falls back to reparent-free pooling if the 64K cap would be hit.

    A single model that alone exceeds the budget still gets its own part —
    the merger warns about the overrun rather than dropping the model.
    """
    if budget is None:
        budget = PartBudget()
    textures: dict[str, set[tuple[str, str]]] = {}
    seq_keys: dict[str, list[tuple[str, float | None, tuple[str, ...]]]] = {}
    for model, parts in pairs:
        textures[model.name] = _texture_keys(model, parts)
        seq_keys[model.name] = _sequence_keys(model)

    def bones_fit(part: list[Pair]) -> bool:
        if model_bones is None or shared is None:
            return True
        # Shared attachment slot bones are appended at merge time.
        reserved = len(shared) + attachment_slots([m for m, _ in part])
        plan = plan_pool(
            {m.name: model_bones[m.name] for m, _ in part}, shared,
            max_slots=budget.bones - reserved,
        )
        return reserved + plan.size <= budget.bones

    out: list[list[Pair]] = []
    current: list[Pair] = []
    for pair in pairs:
        trial = current + [pair]
        submodels, texcount, seqcount = _part_counts(
            trial, textures, seq_keys, shared_hands=shared_hands)
        if current and (submodels > budget.submodels
                        or texcount > budget.textures
                        or seqcount > budget.sequences
                        or not bones_fit(trial)):
            out.append(current)
            current = [pair]
        else:
            current = trial
    if current:
        out.append(current)
    return out


__all__ = ["PartBudget", "SEQUENCE_BUDGET", "SHARED_HANDS_SEQUENCE_BUDGET",
           "SUBMODEL_LIMIT",
           "TEXTURE_BUDGET", "split_parts"]
