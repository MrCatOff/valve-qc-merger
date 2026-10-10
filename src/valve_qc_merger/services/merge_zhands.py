"""merge-zhands as a service (docs/merge-zhands.md): zombie hands -> one model."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.merge_view.discovery import (
    MergeViewError,
    discover_models,
    load_model,
    sanitize_model_dir,
)
from valve_qc_merger.merge_view.merger import write_manifest_data
from valve_qc_merger.merge_zhands.merger import (
    BONE_LIMIT,
    VERTEX_LIMIT,
    ZhandsError,
    hands_size,
    merge_zhands,
    split_zombies,
)
from valve_qc_merger.services.base import (
    EXIT_DISCOVERY,
    EXIT_FAIL,
    EXIT_OK,
    GateRow,
    Reporter,
    ServiceResult,
)
from valve_qc_merger.services.standalone import Rejects
from valve_qc_merger.services.standalone import ship as ship_standalone


@dataclass
class MergeZhandsOptions:
    models_dir: Path
    out: Path
    name: str = "v_zhands"
    exclude: list[str] = field(default_factory=list)
    grenade_prefix: str = "frogbomb"
    grenade_texture: str | None = "frogbomb.bmp"
    manifest_format: str = "ini"
    # a model the merge cannot take ships as a model of its own (standalone/)
    standalone_rejects: bool = True


def run_merge_zhands(opts: MergeZhandsOptions,
                     reporter: Reporter | None = None) -> ServiceResult:
    """Merge every decompiled zombie hand model under ``opts.models_dir``."""
    reporter = reporter or Reporter()
    result = ServiceResult()
    try:
        model_dirs = discover_models(opts.models_dir, exclude=set(opts.exclude))
    except MergeViewError as exc:
        reporter.log(f"error: {exc}")
        result.exit_code = EXIT_DISCOVERY
        return result
    models = []
    for done, model_dir in enumerate(model_dirs):
        reporter.check()
        reporter.progress(done, len(model_dirs), model_dir.name)
        sanitize_model_dir(model_dir)
        try:
            models.append(load_model(model_dir))
        except MergeViewError as exc:
            reporter.log(f"  {model_dir.name:<26} FAIL  {exc}")
            result.failures.append(str(exc))
            result.exit_code = EXIT_DISCOVERY
            return result
        reporter.log(f"  {model_dir.name:<26} OK    sequences={len(models[-1].anims)}")
    rejects = Rejects(opts.standalone_rejects, {d.name: d for d in model_dirs})
    kept = []
    for model in models:
        verts, normals = hands_size(model, opts.grenade_prefix)
        if verts > VERTEX_LIMIT or normals > VERTEX_LIMIT:
            rejects.reject(result, reporter, model.name,
                           f"model {model.name!r}: its hands are {verts} vertices / {normals} "
                           f"normals — over studiomdl's {VERTEX_LIMIT} for one submodel",
                           "TOO-BIG", "hands over 2048 vertices / normals")
        else:
            kept.append(model)
    models = kept
    if not models:
        ship_standalone(rejects, opts.out, opts.manifest_format, result, reporter)
        result.exit_code = EXIT_FAIL if result.failures else EXIT_OK
        if not rejects.reasons:
            reporter.log("error: no models found")
            result.exit_code = EXIT_DISCOVERY
        return result
    reporter.check()
    parts = split_zombies(models, opts.grenade_prefix)
    multi = len(parts) > 1
    if multi:
        reporter.log(f"  split: {len(parts)} parts (studiomdl keeps {BONE_LIMIT} bones "
                     "a model; a zombie's models stay together)")
    result.data["parts"] = len(parts)
    result.data["hands"] = []
    aggregate: dict[str, dict[str, object]] = {}
    passed_all = True
    for number, part in enumerate(parts, 1):
        reporter.check()
        reporter.progress(number - 1, len(parts), f"part {number}")
        part_name = f"{opts.name}_p{number}" if multi else opts.name
        part_out = opts.out / f"p{number}" if multi else opts.out
        try:
            report = merge_zhands(
                part, part_out, part_name,
                grenade_prefix=opts.grenade_prefix,
                grenade_texture=opts.grenade_texture,
                manifest_format=opts.manifest_format,
            )
        except ZhandsError as exc:
            reporter.log(f"error: {exc}")
            result.failures.append(str(exc))
            result.exit_code = EXIT_FAIL
            return result
        for warning in report.warnings:
            reporter.log(f"  [Warning] {warning}")
        reporter.log(f"  {part_name}: bones={report.bones} hands={len(report.hands)} "
                     f"grenade={report.grenade_from} sequences={report.sequences} "
                     f"({report.sequences_deduped} shared) textures={report.textures}")
        reporter.log(f"  hands: {', '.join(report.hands)}")
        for check, passed, detail in report.gate:
            reporter.log(f"    verify {check:<20} {'PASS' if passed else 'FAIL'}  {detail}")
            result.gates.append(GateRow(part_name, check, passed, detail))
        result.outputs.append(part_out / f"{part_name}.qc")
        result.warnings.extend(report.warnings)
        for key, value in report.manifest.items():
            aggregate[key] = ({"model": f"{part_name}.mdl", **dict(value)} if multi
                              else dict(value))
        result.data["hands"] += list(report.hands)
        if not all(passed for _c, passed, _d in report.gate):
            passed_all = False
            result.failures.append(f"{part_name}: verification gate failed")
    reporter.progress(len(parts), len(parts), "done")
    if multi:
        write_manifest_data(opts.out, aggregate, opts.manifest_format)
    result.manifest = aggregate
    ship_standalone(rejects, opts.out, opts.manifest_format, result, reporter)
    result.exit_code = EXIT_OK if passed_all and not result.failures else EXIT_FAIL
    return result


__all__ = ["MergeZhandsOptions", "run_merge_zhands"]
