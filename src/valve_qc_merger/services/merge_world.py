"""merge-w as a service (docs/merge-w.md): decompiled w_ models -> parts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.merge_player.analyze import PlayerPlan
from valve_qc_merger.merge_player.loading import load_player_model
from valve_qc_merger.merge_player.merger import skin_texture_files
from valve_qc_merger.merge_player.parts import (
    TEXTURE_BUDGET,
    PlayerBudget,
    split_player_parts,
)
from valve_qc_merger.merge_view.atlas import TextureOptions
from valve_qc_merger.merge_view.discovery import (
    MergeViewError,
    ModelInput,
    discover_models,
    sanitize_model_dir,
)
from valve_qc_merger.merge_view.merger import MergeError, write_manifest_data
from valve_qc_merger.merge_world.bake import bake_rendered_pose
from valve_qc_merger.merge_world.merger import (
    FLASH_BONE,
    WEAPON_BONE,
    merge_world_models,
)
from valve_qc_merger.merge_world.verify import verify_world_part
from valve_qc_merger.services.base import (
    EXIT_DISCOVERY,
    EXIT_FAIL,
    EXIT_OK,
    GateRow,
    Reporter,
    ServiceResult,
)
from valve_qc_merger.services.standalone import Rejects
from valve_qc_merger.services.standalone import entry as standalone_entry
from valve_qc_merger.services.standalone import ship as ship_standalone


@dataclass
class MergeWorldOptions:
    models_dir: Path
    out: Path
    name: str = "w_merged"
    exclude: list[str] = field(default_factory=list)
    manifest_format: str = "ini"
    texture_budget: int = TEXTURE_BUDGET
    max_texture_size: int | None = None
    pack_textures: bool = False
    no_pack_texture: list[str] = field(default_factory=list)
    no_verify: bool = False
    dry_run: bool = False
    # a model the merge cannot take ships as a model of its own (standalone/)
    standalone_rejects: bool = True


def run_merge_world(opts: MergeWorldOptions,
                    reporter: Reporter | None = None) -> ServiceResult:
    """Merge every decompiled w_ model under ``opts.models_dir``."""
    reporter = reporter or Reporter()
    result = ServiceResult()
    try:
        model_dirs = discover_models(opts.models_dir, exclude=set(opts.exclude))
    except MergeViewError as exc:
        reporter.log(f"error: {exc}")
        result.exit_code = EXIT_DISCOVERY
        return result

    inventory: list[dict[str, object]] = []
    failures = result.failures
    loaded: list[ModelInput] = []
    skin_textures: dict[str, set[str]] = {}
    rejects = Rejects(opts.standalone_rejects, {d.name: d for d in model_dirs})
    for done, model_dir in enumerate(model_dirs):
        reporter.check()
        reporter.progress(done, len(model_dirs), model_dir.name)
        sanitised = sanitize_model_dir(model_dir)
        try:
            model = load_player_model(model_dir)
            plan = bake_rendered_pose(model)
        except (MergeViewError, ValueError) as exc:
            rejects.reject(result, reporter, model_dir.name, str(exc), "FAIL",
                           f"not merged: {exc}")
            continue
        loaded.append(model)
        skin_textures[model.name] = skin_texture_files(model.qc_text)
        warnings = model.warnings + plan.warnings
        inventory.append({
            "name": model.name,
            "source_bones": len(model.bone_names),
            "max_bake_delta": plan.max_bake_delta,
            "sequences": len(model.anims),
            "sanitised": sanitised,
            "warnings": warnings,
        })
        bake = (f" baked {plan.max_bake_delta:.2f}u"
                if plan.max_bake_delta > 1e-4 else "")
        warn = f"  ({len(warnings)} warnings)" if warnings else ""
        reporter.log(f"  {model.name:<20} OK    bones={len(model.bone_names)}"
                     f"{bake}{warn}")
    reporter.progress(len(model_dirs), len(model_dirs), "loaded")

    reporter.log(f"  {'-' * 60}")
    reporter.log(f"  {len(loaded)} models loaded, {len(failures)} failed")
    opts.out.mkdir(parents=True, exist_ok=True)
    (opts.out / "inventory.json").write_text(
        json.dumps({"models": inventory, "failures": failures}, indent=1)
    )
    result.data["inventory"] = inventory
    if opts.dry_run or not loaded:
        if not opts.dry_run:
            ship_standalone(rejects, opts.out, opts.manifest_format, result, reporter,
                            make_entry=_entry)
        result.exit_code = EXIT_FAIL if failures else EXIT_OK
        return result

    # Reuse the player splitter: every model costs the same two bones.
    pairs = [
        (model, PlayerPlan(model=model.name, shared=[FLASH_BONE, WEAPON_BONE]))
        for model in loaded
    ]
    parts = split_player_parts(
        pairs, PlayerBudget(textures=opts.texture_budget), skin_textures=skin_textures,
    )
    multi = len(parts) > 1
    result.data["parts"] = len(parts)
    if multi:
        reporter.log(f"  split: {len(parts)} parts "
                     f"(studiomdl caps one model at 32 submodels)")
    aggregate: dict[str, dict[str, object]] = {}
    for number, part_pairs in enumerate(parts, 1):
        reporter.check()
        reporter.progress(number - 1, len(parts), f"part {number}")
        part_models = [model for model, _plan in part_pairs]
        part_name = f"{opts.name}_p{number}" if multi else opts.name
        part_out = opts.out / f"p{number}" if multi else opts.out
        try:
            report = merge_world_models(
                part_models, part_out, part_name,
                manifest_format=opts.manifest_format,
                write_manifest=not multi,
                textures=TextureOptions(
                    max_size=opts.max_texture_size,
                    pack=opts.pack_textures,
                    no_pack=opts.no_pack_texture,
                ),
            )
        except MergeError as exc:
            reporter.log(f"error: merge failed ({part_name}): {exc}")
            result.exit_code = EXIT_FAIL
            return result
        result.outputs.append(part_out / f"{part_name}.qc")
        result.warnings.extend(report.warnings)
        reporter.log(f"  {part_name}: {len(part_models)} weapons "
                     f"bones={report.bones} textures={report.textures}")
        for warning in report.warnings:
            reporter.log(f"    warn: {warning}")
        for model in part_models:
            aggregate[model.name] = {
                "model": f"{part_name}.mdl",
                **report.manifest[model.name],
            }
        for key, value in report.manifest.items():
            if value.get("skin_of") in aggregate:  # a skin alias of this part
                aggregate[key] = {"model": f"{part_name}.mdl", **value}
        if report.atlas:
            aggregate[f"textures_{part_name}"] = dict(report.atlas)
        if not opts.no_verify:
            gate = verify_world_part(
                part_out, f"{part_name}.qc", opts.models_dir,
                [m.name for m in part_models],
            )
            for row in gate:
                mark = "PASS" if row.passed else "FAIL"
                reporter.log(f"    verify {row.check:<20} {mark}  {row.detail}")
                result.gates.append(GateRow(part_name, row.check, row.passed, row.detail))
            if not all(row.passed for row in gate):
                failures.append(f"{part_name}: verification gate failed")
    reporter.progress(len(parts), len(parts), "done")
    if multi:
        write_manifest_data(opts.out, aggregate, opts.manifest_format)
    result.manifest = aggregate
    ship_standalone(rejects, opts.out, opts.manifest_format, result, reporter,
                    make_entry=_entry)
    result.exit_code = EXIT_FAIL if failures else EXIT_OK
    return result


def _entry(directory: Path, name: str, reason: str) -> dict[str, object]:
    # a dropped weapon plays no sequence the plugin picks: model + pev_body only
    return standalone_entry(directory, name, reason, sequences=False)


__all__ = ["MergeWorldOptions", "run_merge_world"]
