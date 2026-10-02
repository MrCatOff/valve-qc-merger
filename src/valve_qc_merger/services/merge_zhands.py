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
from valve_qc_merger.merge_zhands.merger import ZhandsError, merge_zhands
from valve_qc_merger.services.base import (
    EXIT_DISCOVERY,
    EXIT_FAIL,
    EXIT_OK,
    GateRow,
    Reporter,
    ServiceResult,
)


@dataclass
class MergeZhandsOptions:
    models_dir: Path
    out: Path
    name: str = "v_zhands"
    exclude: list[str] = field(default_factory=list)
    grenade_prefix: str = "frogbomb"
    grenade_texture: str | None = "frogbomb.bmp"
    manifest_format: str = "ini"


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
    if not models:
        reporter.log("error: no models found")
        result.exit_code = EXIT_DISCOVERY
        return result
    reporter.check()
    try:
        report = merge_zhands(
            models, opts.out, opts.name,
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
    reporter.log(f"  {opts.name}: bones={report.bones} hands={len(report.hands)} "
                 f"grenade={report.grenade_from} sequences={report.sequences} "
                 f"({report.sequences_deduped} shared) textures={report.textures}")
    reporter.log(f"  hands: {', '.join(report.hands)}")
    for check, passed, detail in report.gate:
        reporter.log(f"    verify {check:<20} {'PASS' if passed else 'FAIL'}  {detail}")
        result.gates.append(GateRow(opts.name, check, passed, detail))
    reporter.progress(len(model_dirs), len(model_dirs), "done")
    result.outputs.append(opts.out / f"{opts.name}.qc")
    result.warnings.extend(report.warnings)
    result.manifest = {k: dict(v) for k, v in report.manifest.items()}
    result.data["hands"] = list(report.hands)
    passed_all = all(passed for _c, passed, _d in report.gate)
    if not passed_all:
        result.failures.append(f"{opts.name}: verification gate failed")
    result.exit_code = EXIT_OK if passed_all else EXIT_FAIL
    return result


__all__ = ["MergeZhandsOptions", "run_merge_zhands"]
