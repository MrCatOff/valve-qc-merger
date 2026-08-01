"""CLI wiring for merge-view (docs/merge-view-spec.md).

Milestone 1: discovery + sanitisation + loading with a ``--dry-run`` inventory.
Later milestones add hand canonicalisation, pooling, merging and textures; the
flags below are the approved surface, and the not-yet-implemented ones abort
with a clear message rather than silently doing nothing.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.merge_view.animsize import SEQ_DATA_LIMIT
from valve_qc_merger.merge_view.atlas import TextureOptions
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
    discover_models,
    load_model,
    sanitize_model_dir,
)
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
    TEXTURE_BUDGET,
    PartBudget,
    split_parts,
)
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget.config import DEFAULT_REFERENCE
from valve_qc_merger.retarget.correspondence import CorrespondenceError
from valve_qc_merger.writers.smd import write_smd_file

EXIT_OK = 0
EXIT_FAIL = 2
EXIT_DISCOVERY = 3


class MergeViewCommand(Command):
    """Merge decompiled view-models onto one canonical hand skeleton."""

    name = "merge-view"
    help = "merge a folder of decompiled view-models into combined models (spec draft)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("models_dir", type=Path,
                            help="parent directory; each subdir with one .qc is a model")
        parser.add_argument("--out", type=Path, required=True, help="output directory")
        parser.add_argument("--name", default="v_merged", help="output model name stem")
        parser.add_argument("--exclude", action="append", default=[],
                            metavar="NAME", help="skip a model directory (repeatable)")
        parser.add_argument("--reference", type=Path, default=Path(DEFAULT_REFERENCE),
                            help="canonical hand skeleton SMD")
        parser.add_argument("--skip-unmatched", action="store_true",
                            help="continue past models whose rig cannot be matched")
        parser.add_argument("--prune", action="store_true",
                            help="also fold away vertex-less unreferenced bones "
                                 "(default keeps everything except Finger*Nub)")
        parser.add_argument("--no-pool-bones", action="store_true",
                            help="skip bone pooling (merged table may exceed 127)")
        parser.add_argument("--manifest-format", choices=("ini", "json", "toml"),
                            default="ini", help="per-model manifest format")
        parser.add_argument("--texture-budget", type=int, default=TEXTURE_BUDGET,
                            help="max textures per compiled part (default "
                                 f"{TEXTURE_BUDGET}; hard engine cap is 100)")
        parser.add_argument("--sequence-budget", type=int, default=SEQUENCE_BUDGET,
                            help="max sequences per compiled part after "
                                 f"dedupe (default {SEQUENCE_BUDGET})")
        parser.add_argument("--max-texture-size", type=int, metavar="N",
                            help="downscale staged textures larger than N on "
                                 "either axis (8-bit re-quantised)")
        parser.add_argument("--pack-textures", action="store_true",
                            help="pack eligible textures four-to-a-file into "
                                 "512x512 atlases (shared palette, UVs "
                                 "rewritten; chrome/additive/tiling excluded)")
        parser.add_argument("--no-pack-texture", action="append", default=[],
                            metavar="GLOB",
                            help="keep matching textures out of atlases "
                                 "(repeatable)")
        parser.add_argument("--dry-run", action="store_true",
                            help="discover, sanitise and load only; print the inventory")

    def run(self, args: argparse.Namespace) -> int:
        try:
            model_dirs = discover_models(args.models_dir, exclude=set(args.exclude))
        except MergeViewError as exc:
            print(f"error: {exc}")
            return EXIT_DISCOVERY

        try:
            reference = load_reference_rig(args.reference)
        except (OSError, CorrespondenceError) as exc:
            print(f"error: reference hands unusable: {exc}")
            return EXIT_DISCOVERY

        inventory: list[dict[str, object]] = []
        failures: list[str] = []
        merged_pairs: list[tuple[ModelInput, ModelParts]] = []
        for model_dir in model_dirs:
            sanitised = sanitize_model_dir(model_dir)
            try:
                model = load_model(model_dir)
            except MergeViewError as exc:
                failures.append(str(exc))
                print(f"  {model_dir.name:<20} FAIL  {exc}")
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
                message = f"model {model.name!r}: {exc}"
                failures.append(message)
                print(f"  {model.name:<20} UNMATCHED  {exc}")
                if not args.skip_unmatched:
                    continue
                continue
            already = sum(1 for old, new in match.renames.items() if old == new)
            canonical: dict[str, object] = {}
            if not args.dry_run:
                reference_nodes = parse_smd_file(args.reference).nodes
                result = canonicalize_model(
                    model, match, reference_nodes, prune=args.prune
                )
                if result.max_pose_deviation > 1e-4:
                    message = (f"model {model.name!r}: pose NOT preserved "
                               f"(deviation {result.max_pose_deviation:.6f}u)")
                    failures.append(message)
                    print(f"  {model.name:<20} POSE-FAIL  {message}")
                    continue
                out_model = args.out / "canonical" / model.name
                (out_model / "anims").mkdir(parents=True, exist_ok=True)
                for stem, mesh_smd in model.meshes.items():
                    target = out_model / (Path(stem.replace("\\", "/")).name + ".smd")
                    write_smd_file(mesh_smd, target)
                for seq_name, anim_smd in model.anims.items():
                    write_smd_file(anim_smd, out_model / "anims" / f"{seq_name}.smd")
                (out_model / model.qc_path.name).write_text(model.qc_text,
                                                            encoding="latin-1")
                parts = collapse_bodygroups(model)
                merged_pairs.append((model, parts))
                canonical = {
                    "renamed": result.renamed,
                    "reparented": result.reparented,
                    "nubs_removed": result.nubs_removed,
                    "pruned": len(result.pruned),
                    "max_pose_deviation": result.max_pose_deviation,
                    "canonical_warnings": result.warnings + parts.warnings,
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
            print(f"  {model.name:<20} OK    bones={entry['bones']:<4} "
                  f"mapped={len(match.renames):<3} sequences={entry['sequences']}{san}{warn}")

        print(f"  {'-' * 60}")
        print(f"  {len(inventory)} models loaded, {len(failures)} failed")

        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "inventory.json").write_text(
            json.dumps({"models": inventory, "failures": failures}, indent=1)
        )
        if not args.dry_run and merged_pairs:
            reference_smd = parse_smd_file(args.reference)
            shared = {n.name for n in reference_smd.nodes
                      if not n.name.endswith("Nub")}
            all_bones: dict[str, dict[str, str | None]] = {}
            for model, _parts in merged_pairs:
                fullest = max(model.meshes.values(), key=lambda m: len(m.nodes))
                name_of = {n.index: n.name for n in fullest.nodes}
                all_bones[model.name] = {
                    n.name: (name_of.get(n.parent) if n.parent >= 0 else None)
                    for n in fullest.nodes
                }
            part_split = split_parts(
                merged_pairs,
                PartBudget(textures=args.texture_budget,
                           sequences=args.sequence_budget),
                model_bones=all_bones, shared=shared,
            )

            # Resolve each part's pooling BEFORE writing anything: prefer
            # structure-matched pooling (prior-art; ~90 slots for a dozen
            # weapons), but preview the pooled sequence sizes with the exact
            # studiomdl replica — if a reparent would blow the 64K cap, fall
            # back to reparent-free pooling, halving the part if that then
            # exceeds the bone budget.
            resolved: list[tuple[list[tuple[ModelInput, ModelParts]],
                                 PoolPlan | None, str]] = []
            queue = list(part_split)
            while queue:
                part_pairs = queue.pop(0)
                if args.no_pool_bones:
                    resolved.append((part_pairs, None, "unpooled"))
                    continue
                model_bones = {
                    model.name: all_bones[model.name]
                    for model, _parts in part_pairs
                }
                plan = plan_pool(model_bones, shared,
                                 max_slots=BONE_LIMIT - len(shared))
                sizes = preview_pool_sizes(part_pairs, plan)
                if max(sizes.values(), default=0) <= SEQ_DATA_LIMIT:
                    resolved.append((part_pairs, plan, "pooled"))
                    continue
                plan = plan_pool(model_bones, shared, allow_reparent=False)
                if len(shared) + plan.size <= BONE_LIMIT or len(part_pairs) == 1:
                    resolved.append((part_pairs, plan, "rename-only"))
                    continue
                mid = len(part_pairs) // 2
                queue = [part_pairs[:mid], part_pairs[mid:]] + queue

            multi = len(resolved) > 1
            if multi:
                print(f"  split: {len(resolved)} parts "
                      f"(studiomdl caps one model at 32 submodels)")
            aggregate: dict[str, dict[str, object]] = {}
            for number, (part_pairs, part_plan, mode) in enumerate(resolved, 1):
                part_name = f"{args.name}_p{number}" if multi else args.name
                part_out = args.out / f"p{number}" if multi else args.out
                if part_plan is not None:
                    pooled = 0
                    for model, _parts in part_pairs:
                        pooled += len(apply_pool(
                            model, part_plan.assignments[model.name],
                            part_plan.slot_parent,
                        ))
                    print(f"  {part_name}: {len(part_pairs)} models, pool "
                          f"{part_plan.size} slots ({mode}), {pooled} reparents")
                try:
                    report = merge_models(
                        part_pairs, part_out, part_name,
                        manifest_format=args.manifest_format,
                        write_manifest=not multi,
                        textures=TextureOptions(
                            max_size=args.max_texture_size,
                            pack=args.pack_textures,
                            no_pack=args.no_pack_texture,
                        ),
                    )
                except MergeError as exc:
                    print(f"error: merge failed ({part_name}): {exc}")
                    return EXIT_FAIL
                shared_note = (f" ({report.sequences_deduped} shared)"
                               if report.sequences_deduped else "")
                print(f"  {part_name}: bones={report.bones} "
                      f"bodyparts={report.bodyparts} "
                      f"sequences={report.sequences}{shared_note} "
                      f"textures={report.textures}")
                for warning in report.warnings:
                    print(f"    warn: {warning}")
                for model, _parts in part_pairs:
                    aggregate[model.name] = {
                        "model": f"{part_name}.mdl",
                        **report.manifest[model.name],
                    }
                if report.atlas:
                    aggregate[f"textures_{part_name}"] = dict(report.atlas)
            if multi:
                write_manifest_data(args.out, aggregate, args.manifest_format)
        return EXIT_FAIL if failures else EXIT_OK


__all__ = ["MergeViewCommand"]
