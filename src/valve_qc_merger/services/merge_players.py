"""merge-players as a service (docs/merge-players.md): CSO bodies -> skins."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.merge_players.discovery import (
    PlayerModel,
    load_donor,
    load_player_body,
)
from valve_qc_merger.merge_players.grouping import group_models
from valve_qc_merger.merge_players.merger import merge_players_part
from valve_qc_merger.merge_players.parts import (
    DEFAULT_SUBMODEL_LIMIT,
    TEXTURE_BUDGET,
    split_parts,
)
from valve_qc_merger.merge_players.sequences import DEFAULT_PLACEHOLDER_GLOBS
from valve_qc_merger.merge_players.verify import verify_players_part
from valve_qc_merger.merge_view.atlas import TextureOptions
from valve_qc_merger.merge_view.discovery import (
    MergeViewError,
    discover_models,
    sanitize_model_dir,
)
from valve_qc_merger.merge_view.merger import MergeError, write_manifest_data
from valve_qc_merger.services.base import (
    EXIT_DISCOVERY,
    EXIT_FAIL,
    EXIT_OK,
    GateRow,
    Reporter,
    ServiceResult,
)


def _slug(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_") or "group"


@dataclass
class MergePlayersOptions:
    models_dir: Path
    out: Path
    name: str = "players"
    base: Path = Path("storage/players_donor")
    group_by: str = "size"
    proportion_tolerance: float = 2.0
    labels: Path | None = None
    placeholder_seq: list[str] = field(default_factory=list)
    include_base: bool = False
    max_skins: int | None = None
    submodel_limit: int = DEFAULT_SUBMODEL_LIMIT
    exclude: list[str] = field(default_factory=list)
    manifest_format: str = "ini"
    texture_budget: int = TEXTURE_BUDGET
    max_texture_size: int | None = None
    pack_textures: bool = False
    no_pack_texture: list[str] = field(default_factory=list)
    no_verify: bool = False
    dry_run: bool = False


def _load_labels(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    import tomllib
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    return {str(k): str(v) for k, v in data.items()}


def run_merge_players(opts: MergePlayersOptions,  # noqa: C901 - orchestration
                      reporter: Reporter | None = None) -> ServiceResult:
    """Merge CSO player bodies under ``opts.models_dir`` onto the donor rig."""
    reporter = reporter or Reporter()
    result = ServiceResult()
    globs = tuple(opts.placeholder_seq) or DEFAULT_PLACEHOLDER_GLOBS
    labels = _load_labels(opts.labels)

    try:
        donor = load_donor(opts.base)
    except MergeViewError as exc:
        reporter.log(f"error: donor: {exc}")
        result.exit_code = EXIT_DISCOVERY
        return result
    try:
        model_dirs = discover_models(opts.models_dir, exclude=set(opts.exclude))
    except MergeViewError as exc:
        reporter.log(f"error: {exc}")
        result.exit_code = EXIT_DISCOVERY
        return result

    models: list[PlayerModel] = []
    failures = result.failures
    inventory: list[dict[str, object]] = []
    for done, model_dir in enumerate(model_dirs):
        reporter.check()
        reporter.progress(done, len(model_dirs), model_dir.name)
        sanitize_model_dir(model_dir)
        try:
            model = load_player_body(model_dir)
        except (MergeViewError, ValueError) as exc:
            failures.append(str(exc))
            reporter.log(f"  {model_dir.name:<24} FAIL  {exc}")
            continue
        models.append(model)
        inventory.append({"name": model.name, "hitbox_sig": model.hitbox_sig,
                          "height": model.height, "warnings": model.warnings})
    reporter.progress(len(model_dirs), len(model_dirs), "loaded")

    reporter.log(f"  {'-' * 62}")
    reporter.log(f"  {len(models)} models loaded, {len(failures)} failed; "
                 f"donor {opts.base.name} ({len(donor.table)} bones)")
    opts.out.mkdir(parents=True, exist_ok=True)
    (opts.out / "inventory.json").write_text(
        json.dumps({"models": inventory, "failures": failures}, indent=1)
    )
    result.data["inventory"] = inventory
    if not models:
        result.exit_code = EXIT_FAIL
        return result

    groups = group_models(models, mode=opts.group_by,
                          proportion_tolerance=opts.proportion_tolerance,
                          labels=labels)

    def parts_of(members: list[PlayerModel]) -> list[list[PlayerModel]]:
        return split_parts(members, submodel_limit=opts.submodel_limit,
                           texture_budget=opts.texture_budget,
                           max_skins=opts.max_skins,
                           reserve_submodels=1 if opts.include_base else 0)

    reporter.log(f"  group-by {opts.group_by}: {len(groups)} group(s)")
    for key, members in groups:
        parts = parts_of(members)
        suffix = f" -> {len(parts)} parts" if len(parts) > 1 else ""
        reporter.log(f"    {key:<20} {len(members)} skins{suffix}: "
                     f"{', '.join(m.name for m in members[:6])}"
                     f"{'...' if len(members) > 6 else ''}")
    if opts.dry_run:
        result.exit_code = EXIT_FAIL if failures else EXIT_OK
        return result

    textures = TextureOptions(max_size=opts.max_texture_size,
                              pack=opts.pack_textures, no_pack=opts.no_pack_texture)
    aggregate: dict[str, dict[str, object]] = {}
    for gnum, (key, members) in enumerate(groups):
        reporter.check()
        reporter.progress(gnum, len(groups), key)
        gslug = _slug(key)
        parts = parts_of(members)
        multi = len(parts) > 1
        for pnum, part in enumerate(parts, 1):
            stem = f"{opts.name}_{gslug}" + (f"_p{pnum}" if multi else "")
            part_out = opts.out / gslug / (f"p{pnum}" if multi else "")
            include_base = opts.include_base and pnum == 1
            try:
                report = merge_players_part(
                    part, donor, part_out, stem,
                    include_base=include_base, placeholder_globs=globs,
                    submodel_limit=opts.submodel_limit, textures=textures,
                    manifest_format=opts.manifest_format, write_manifest=True,
                )
            except MergeError as exc:
                reporter.log(f"error: merge failed ({stem}): {exc}")
                result.exit_code = EXIT_FAIL
                return result
            result.outputs.append(part_out / f"{stem}.qc")
            result.warnings.extend(report.warnings)
            reporter.log(f"    {stem}: {len(part)} skins bones={report.bones} "
                         f"seqs={report.sequences} (voided {report.sequences_deduped}) "
                         f"textures={report.textures}")
            for warning in report.warnings:
                reporter.log(f"      warn: {warning}")
            for skin, body in report.manifest.items():
                aggregate[f"{gslug}/{skin}"] = {"model": f"{stem}.mdl", **body}

            if not opts.no_verify:
                skins = ([("base", donor.directory, [donor.body_stem])]
                         if include_base else []) + \
                        [(m.name, m.directory, m.body_stems) for m in part]
                gate = verify_players_part(
                    part_out, f"{stem}.qc", donor, skins,
                    placeholder_globs=globs, submodel_limit=opts.submodel_limit,
                    texture_count=report.textures,
                )
                for row in gate:
                    mark = "PASS" if row.passed else "FAIL"
                    reporter.log(f"      verify {row.check:<20} {mark}  {row.detail}")
                    result.gates.append(GateRow(stem, row.check, row.passed, row.detail))
                if not all(row.passed for row in gate):
                    failures.append(f"{stem}: verification gate failed")
    reporter.progress(len(groups), len(groups), "done")

    write_manifest_data(opts.out, aggregate, opts.manifest_format)
    result.manifest = aggregate
    result.exit_code = EXIT_FAIL if failures else EXIT_OK
    return result


__all__ = ["MergePlayersOptions", "run_merge_players"]
