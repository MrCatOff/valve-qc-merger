"""merge-v as a service (docs/merge-v.md): decompiled view-models -> parts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.merge_view.animsize import SEQ_DATA_LIMIT
from valve_qc_merger.merge_view.atlas import TextureOptions
from valve_qc_merger.merge_view.attachments import attachment_slots
from valve_qc_merger.merge_view.bodygroups import ModelParts, collapse_bodygroups
from valve_qc_merger.merge_view.bonepool import (
    PoolPlan,
    apply_pool,
    plan_pool,
    preview_pool_sizes,
)
from valve_qc_merger.merge_view.canonicalize import canonicalize_model
from valve_qc_merger.merge_view.discovery import (
    MergeViewError,
    ModelInput,
    _resolve_smd,
    discover_models,
    load_model,
    sanitize_model_dir,
)
from valve_qc_merger.merge_view.handcheck import TOLERANCE as HAND_TOLERANCE
from valve_qc_merger.merge_view.handcheck import group_by_hands, hand_shape
from valve_qc_merger.merge_view.hands import (
    collision_guard,
    hand_bone_names,
    load_reference_rig,
    match_hands,
)
from valve_qc_merger.merge_view.merger import (
    BONE_LIMIT,
    MergeError,
    merge_models,
    write_manifest_data,
)
from valve_qc_merger.merge_view.parts import (
    SEQUENCE_BUDGET,
    SHARED_HANDS_SEQUENCE_BUDGET,
    TEXTURE_BUDGET,
    PartBudget,
    split_parts,
)
from valve_qc_merger.merge_view.verify import verify_part
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.resources import resource_path
from valve_qc_merger.retarget.config import (
    DEFAULT_REFERENCE,
    DEFAULT_SHARED_HANDS_REFERENCE,
)
from valve_qc_merger.retarget.correspondence import CorrespondenceError
from valve_qc_merger.services.base import (
    EXIT_DISCOVERY,
    EXIT_FAIL,
    EXIT_OK,
    GateRow,
    Reporter,
    ServiceResult,
)
from valve_qc_merger.writers.smd import write_smd_file


@dataclass
class MergeViewOptions:
    models_dir: Path
    out: Path
    name: str = "v_merged"
    exclude: list[str] = field(default_factory=list)
    reference: Path | None = None  # None: the ValveBiped/CSO default per mode
    skip_unmatched: bool = False
    shared_hands: bool = False
    prune: bool = False
    no_pool_bones: bool = False
    manifest_format: str = "ini"
    texture_budget: int = TEXTURE_BUDGET
    sequence_budget: int | None = None  # None: 111, or 255 with shared_hands
    max_texture_size: int | None = None
    pack_textures: bool = False
    no_pack_texture: list[str] = field(default_factory=list)
    sound_path: str | None = None
    max_decimation: float = 0.15  # --shared-hands: fold multi-part weapons
    no_verify: bool = False
    dry_run: bool = False


def run_merge_view(opts: MergeViewOptions, reporter: Reporter | None = None) -> ServiceResult:
    """Merge every decompiled view-model under ``opts.models_dir``."""
    reporter = reporter or Reporter()
    result = ServiceResult()
    reference_path = resource_path(
        opts.reference if opts.reference is not None else Path(
            DEFAULT_SHARED_HANDS_REFERENCE if opts.shared_hands else DEFAULT_REFERENCE)
    )
    sequence_budget = (opts.sequence_budget if opts.sequence_budget is not None
                       else SHARED_HANDS_SEQUENCE_BUDGET if opts.shared_hands
                       else SEQUENCE_BUDGET)
    try:
        model_dirs = discover_models(opts.models_dir, exclude=set(opts.exclude))
    except MergeViewError as exc:
        reporter.log(f"error: {exc}")
        result.exit_code = EXIT_DISCOVERY
        return result

    try:
        reference = load_reference_rig(reference_path)
    except (OSError, CorrespondenceError) as exc:
        reporter.log(f"error: reference hands unusable: {exc}")
        result.exit_code = EXIT_DISCOVERY
        return result

    inventory: list[dict[str, object]] = []
    failures = result.failures
    merged_pairs: list[tuple[ModelInput, ModelParts]] = []
    hand_renames: dict[str, dict[str, str]] = {}
    original_anims: dict[str, dict[str, str]] = {}
    original_meshes: dict[str, list[str]] = {}
    for done, model_dir in enumerate(model_dirs):
        reporter.check()
        reporter.progress(done, len(model_dirs), model_dir.name)
        sanitised = sanitize_model_dir(model_dir)
        try:
            model = load_model(model_dir)
        except MergeViewError as exc:
            failures.append(str(exc))
            reporter.log(f"  {model_dir.name:<20} FAIL  {exc}")
            continue
        fullest = max(model.meshes.values(), key=lambda m: len(m.nodes))
        include = hand_bone_names(model.meshes, model.bodygroups)
        try:
            match = match_hands(fullest, reference, include)
            conflicts = collision_guard(fullest, match.renames)
            if conflicts:
                raise CorrespondenceError(
                    f"rename collisions: {'; '.join(conflicts)}"
                )
        except CorrespondenceError as exc:
            failures.append(f"model {model.name!r}: {exc}")
            reporter.log(f"  {model.name:<20} UNMATCHED  {exc}")
            continue
        already = sum(1 for old, new in match.renames.items() if old == new)
        canonical: dict[str, object] = {}
        if not opts.dry_run:
            reference_nodes = parse_smd_file(reference_path).nodes
            canon = canonicalize_model(
                model, match, reference_nodes, prune=opts.prune
            )
            if canon.max_pose_deviation > 1e-4:
                message = (f"model {model.name!r}: pose NOT preserved "
                           f"(deviation {canon.max_pose_deviation:.6f}u)")
                failures.append(message)
                reporter.log(f"  {model.name:<20} POSE-FAIL  {message}")
                continue
            out_model = opts.out / "canonical" / model.name
            (out_model / "anims").mkdir(parents=True, exist_ok=True)
            for stem, mesh_smd in model.meshes.items():
                target = out_model / (Path(stem.replace("\\", "/")).name + ".smd")
                write_smd_file(mesh_smd, target)
            for seq_name, anim_smd in model.anims.items():
                write_smd_file(anim_smd, out_model / "anims" / f"{seq_name}.smd")
            (out_model / model.qc_path.name).write_text(model.qc_text,
                                                        encoding="latin-1")
            parts = collapse_bodygroups(
                model, max_decimation=opts.max_decimation if opts.shared_hands else 0.0)
            if parts.fold_report is not None:
                reporter.log(f"  {model.name:<20} {parts.warnings[-1]}")
            if opts.shared_hands and len(parts.weapon_stems) > 1:
                # A multi-part weapon needs an extra weapon bodygroup, which
                # multiplies pev_body past the 255 WRITE_BYTE ceiling once
                # merged. Reject it — it must be shipped on its own.
                reporter.log(f"  [Warning] Зброя {model.name} була відхилена по "
                             "причині того що складається з декількох частин, "
                             "можливість вийти за межі ліміту, використовуйте "
                             "індивідуально")
                failures.append(
                    f"model {model.name!r}: multi-part weapon rejected "
                    "(--shared-hands)"
                )
                continue
            merged_pairs.append((model, parts))
            hand_renames[model.name] = dict(match.renames)
            original_meshes[model.name] = [
                str(_resolve_smd(model_dir, stem)) for stem in model.meshes
                if stem not in parts.synthetic
            ]
            original_anims[model.name] = {
                seq.name: str(_resolve_smd(model_dir, seq.smd))
                for seq in model.sequences
                if seq.smd is not None and seq.name in model.anims
            }
            canonical = {
                "renamed": canon.renamed,
                "reparented": canon.reparented,
                "nubs_removed": canon.nubs_removed,
                "pruned": len(canon.pruned),
                "max_pose_deviation": canon.max_pose_deviation,
                "canonical_warnings": canon.warnings + parts.warnings,
            }
        entry = {
            "name": model.name,
            "bones": len(model.bone_names),
            "meshes": len(model.meshes),
            "sequences": len(model.anims),
            "hand_renames": dict(sorted(match.renames.items())),
            "already_canonical": already,
            "held_reference": match.held_reference,
            "match_warnings": match.warnings,
            "sanitised": sanitised,
            "warnings": model.warnings,
            **canonical,
        }
        inventory.append(entry)
        warn = f"  ({len(model.warnings)} warnings)" if model.warnings else ""
        san = f"  ({len(sanitised)} files sanitised)" if sanitised else ""
        reporter.log(f"  {model.name:<20} OK    bones={entry['bones']:<4} "
                     f"mapped={len(match.renames):<3} "
                     f"sequences={entry['sequences']}{san}{warn}")
    reporter.progress(len(model_dirs), len(model_dirs), "loaded")
    if opts.shared_hands and len(merged_pairs) > 1:
        merged_pairs = _drop_foreign_hands(merged_pairs, failures, reporter, result)

    reporter.log(f"  {'-' * 60}")
    reporter.log(f"  {len(inventory)} models loaded, {len(failures)} failed")
    opts.out.mkdir(parents=True, exist_ok=True)
    (opts.out / "inventory.json").write_text(
        json.dumps({"models": inventory, "failures": failures}, indent=1)
    )
    result.data["inventory"] = inventory
    if not opts.dry_run and merged_pairs:
        code = _merge_parts(opts, reporter, result, merged_pairs, reference_path,
                            sequence_budget, hand_renames, original_anims,
                            original_meshes)
        if code is not None:
            result.exit_code = code
            return result
    result.exit_code = EXIT_FAIL if failures else EXIT_OK
    return result


def _drop_foreign_hands(
    pairs: list[tuple[ModelInput, ModelParts]],
    failures: list[str],
    reporter: Reporter,
    result: ServiceResult,
) -> list[tuple[ModelInput, ModelParts]]:
    """--shared-hands emits the first model's hands for every weapon: keep
    only the models wearing the most common hands (bone-local comparison),
    reject the rest — typically a model that was never retargeted."""
    entries = [(model.name, [hand_shape(model.meshes[s]) for s in parts.hand_variants])
               for model, parts in pairs]
    clusters = group_by_hands(entries)
    keep = set(clusters[0])
    for cluster in clusters[1:]:
        for name in cluster:
            message = (f"model {name!r}: wears other hands than the {len(keep)} other "
                       "model(s) (not retargeted?) — rejected under --shared-hands; "
                       "retarget it first or merge it without --shared-hands")
            failures.append(message)
            reporter.log(f"  {name:<20} HANDS-MISMATCH  {message}")
    rejected = len(pairs) - len(keep)
    result.gates.append(GateRow(
        "inputs", "shared_hands", True,
        f"{len(keep)} model(s) wear identical hands (bone-local <= {HAND_TOLERANCE}u)"
        + (f"; {rejected} rejected" if rejected else "")))
    reporter.log(f"  shared hands: {len(keep)} model(s) agree"
                 + (f", {rejected} rejected" if rejected else ""))
    return [(model, parts) for model, parts in pairs if model.name in keep]


def _merge_parts(
    opts: MergeViewOptions,
    reporter: Reporter,
    result: ServiceResult,
    merged_pairs: list[tuple[ModelInput, ModelParts]],
    reference_path: Path,
    sequence_budget: int,
    hand_renames: dict[str, dict[str, str]],
    original_anims: dict[str, dict[str, str]],
    original_meshes: dict[str, list[str]],
) -> int | None:
    """Split, pool, merge and verify; returns an exit code to stop early."""
    failures = result.failures
    reference_smd = parse_smd_file(reference_path)
    shared = {n.name for n in reference_smd.nodes if not n.name.endswith("Nub")}
    all_bones: dict[str, dict[str, str | None]] = {}
    for model, _parts in merged_pairs:
        fullest = max(model.meshes.values(), key=lambda m: len(m.nodes))
        name_of = {n.index: n.name for n in fullest.nodes}
        all_bones[model.name] = {
            n.name: (name_of.get(n.parent) if n.parent >= 0 else None)
            for n in fullest.nodes
        }
    budget = PartBudget(textures=opts.texture_budget, sequences=sequence_budget)
    part_split = split_parts(merged_pairs, budget, model_bones=all_bones,
                             shared=shared, shared_hands=opts.shared_hands)

    # Resolve each part's pooling BEFORE writing anything: prefer
    # structure-matched pooling (prior-art; ~90 slots for a dozen weapons),
    # but preview the pooled sequence sizes with the exact studiomdl replica —
    # if a reparent would blow the 64K cap, fall back to reparent-free
    # pooling. A part that fits neither keeps its longest fitting prefix
    # (binary search) and spills the rest, re-packed once the queue drains
    # (halving stranded small parts: 5 parts became 8 once attachment slots
    # tightened the budget).
    def resolve_pool(
        part_pairs: list[tuple[ModelInput, ModelParts]],
    ) -> tuple[PoolPlan, str] | None:
        model_bones = {model.name: all_bones[model.name] for model, _p in part_pairs}
        # Shared attachment slot bones are appended at merge time.
        reserved = len(shared) + attachment_slots([m for m, _p in part_pairs])
        plan = plan_pool(model_bones, shared, max_slots=BONE_LIMIT - reserved)
        sizes = preview_pool_sizes(part_pairs, plan)
        if max(sizes.values(), default=0) <= SEQ_DATA_LIMIT:
            return plan, "pooled"
        plan = plan_pool(model_bones, shared, allow_reparent=False)
        if reserved + plan.size <= BONE_LIMIT or len(part_pairs) == 1:
            return plan, "rename-only"
        return None

    resolved: list[tuple[list[tuple[ModelInput, ModelParts]],
                         PoolPlan | None, str]] = []
    queue = list(part_split)
    spill: list[tuple[ModelInput, ModelParts]] = []
    while queue or spill:
        reporter.check()
        if not queue:
            queue = split_parts(spill, budget, model_bones=all_bones,
                                shared=shared, shared_hands=opts.shared_hands)
            spill = []
        part_pairs = queue.pop(0)
        if opts.no_pool_bones:
            resolved.append((part_pairs, None, "unpooled"))
            continue
        found = resolve_pool(part_pairs)
        if found is None:
            # Longest fitting prefix; a single model always fits.
            low, high = 1, len(part_pairs) - 1
            found = resolve_pool(part_pairs[:1])
            while low < high:
                mid = (low + high + 1) // 2
                trial = resolve_pool(part_pairs[:mid])
                if trial is not None:
                    low, found = mid, trial
                else:
                    high = mid - 1
            spill = part_pairs[low:] + spill
            part_pairs = part_pairs[:low]
        assert found is not None
        resolved.append((part_pairs, found[0], found[1]))

    multi = len(resolved) > 1
    if multi:
        reporter.log(f"  split: {len(resolved)} parts "
                     f"(studiomdl caps one model at 32 submodels)")
    aggregate: dict[str, dict[str, object]] = {}
    for number, (part_pairs, part_plan, mode) in enumerate(resolved, 1):
        reporter.check()
        reporter.progress(number - 1, len(resolved), f"part {number}")
        part_name = f"{opts.name}_p{number}" if multi else opts.name
        part_out = opts.out / f"p{number}" if multi else opts.out
        if part_plan is not None:
            pooled = 0
            for model, _parts in part_pairs:
                pooled += len(apply_pool(
                    model, part_plan.assignments[model.name], part_plan.slot_parent,
                ))
            reporter.log(f"  {part_name}: {len(part_pairs)} models, pool "
                         f"{part_plan.size} slots ({mode}), {pooled} reparents")
        try:
            report = merge_models(
                part_pairs, part_out, part_name,
                manifest_format=opts.manifest_format,
                write_manifest=not multi,
                textures=TextureOptions(
                    max_size=opts.max_texture_size,
                    pack=opts.pack_textures,
                    no_pack=opts.no_pack_texture,
                ),
                sound_path=opts.sound_path,
                shared_hands=opts.shared_hands,
            )
        except MergeError as exc:
            reporter.log(f"error: merge failed ({part_name}): {exc}")
            return EXIT_FAIL
        result.outputs.append(part_out / f"{part_name}.qc")
        result.warnings.extend(report.warnings)
        shared_note = (f" ({report.sequences_deduped} shared)"
                       if report.sequences_deduped else "")
        reporter.log(f"  {part_name}: bones={report.bones} "
                     f"bodyparts={report.bodyparts} "
                     f"sequences={report.sequences}{shared_note} "
                     f"textures={report.textures}")
        for warning in report.warnings:
            reporter.log(f"    warn: {warning}")
        for model, _parts in part_pairs:
            aggregate[model.name] = {
                "model": f"{part_name}.mdl",
                **report.manifest[model.name],
            }
        if report.atlas:
            aggregate[f"textures_{part_name}"] = dict(report.atlas)
        if not opts.no_verify:
            bone_maps: dict[str, dict[str, str]] = {}
            for model, _parts in part_pairs:
                renames = hand_renames.get(model.name, {})
                assignment = (part_plan.assignments.get(model.name, {})
                              if part_plan is not None else {})
                bone_maps[model.name] = {
                    orig: assignment.get(renames.get(orig, orig),
                                         renames.get(orig, orig))
                    for orig in all_bones[model.name]
                }
            manifest_anim = {
                model.name: {
                    key[5:]: int(value)
                    for key, value in report.manifest[model.name].items()
                    if key.startswith("anim_")
                }
                for model, _parts in part_pairs
            }
            gate = verify_part(
                part_out, f"{part_name}.qc", opts.models_dir,
                [m.name for m, _ in part_pairs], bone_maps,
                manifest_anim, reference_path, original_anims,
                original_meshes,
            )
            for row in gate:
                mark = "PASS" if row.passed else "FAIL"
                reporter.log(f"    verify {row.check:<20} {mark}  {row.detail}")
                result.gates.append(GateRow(part_name, row.check, row.passed, row.detail))
            if not all(row.passed for row in gate):
                failures.append(f"{part_name}: verification gate failed")
    reporter.progress(len(resolved), len(resolved), "done")
    if multi:
        write_manifest_data(opts.out, aggregate, opts.manifest_format)
    result.manifest = aggregate
    return None


__all__ = ["MergeViewOptions", "run_merge_view"]
