"""CLI wiring for merge-players.

Merges decompiled CSO player-character models into skin-bodygrouped CS 1.6
models: a donor rig (arctic) supplies the skeleton, the canonical animation set
(unneeded slots voided with a placeholder) and hitboxes; each source body is one
entry of a single ``skin`` bodygroup. Models are grouped by size / team / sex and
each group split into parts under the per-bodypart submodel limit.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from valve_qc_merger.commands.base import Command
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

EXIT_OK = 0
EXIT_FAIL = 2
EXIT_DISCOVERY = 3


def _slug(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_") or "group"


class MergePlayersCommand(Command):
    """Merge decompiled CSO player-character models into skin-bodygrouped models."""

    name = "merge-players"
    help = "merge CSO player-character models into skin-bodygrouped CS 1.6 models"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("models_dir", type=Path,
                            help="parent directory; each subdir with one .qc is a model")
        parser.add_argument("--out", type=Path, required=True, help="output directory")
        parser.add_argument("--name", default="players", help="output model name stem")
        parser.add_argument("--base", type=Path, default=Path("tmp/ORIGINAL_CS_MODEL"),
                            help="donor rig directory (skeleton + canonical animations)")
        parser.add_argument("--group-by", choices=("size", "team", "sex"),
                            default="size", help="how to partition models into merges")
        parser.add_argument("--proportion-tolerance", type=float, default=2.0,
                            metavar="UNITS",
                            help="size mode: max per-bone length difference (units) "
                                 "for two rigs to share a group (default 2.0)")
        parser.add_argument("--labels", type=Path, metavar="TOML",
                            help="override team/sex labels: {model = \"ct\"|\"t\"|...}")
        parser.add_argument("--placeholder-seq", action="append", default=[],
                            metavar="GLOB",
                            help="void matching sequence slots with a placeholder "
                                 f"(repeatable; default {DEFAULT_PLACEHOLDER_GLOBS[0]!r})")
        parser.add_argument("--include-base", action="store_true",
                            help="include the donor body as skin 0")
        parser.add_argument("--max-skins", type=int, metavar="N",
                            help="cap skins per output part")
        parser.add_argument("--submodel-limit", type=int, default=DEFAULT_SUBMODEL_LIMIT,
                            help="per-bodypart submodel cap (default "
                                 f"{DEFAULT_SUBMODEL_LIMIT}; raise for a patched compiler)")
        parser.add_argument("--exclude", action="append", default=[],
                            metavar="NAME", help="skip a model directory (repeatable)")
        parser.add_argument("--manifest-format", choices=("ini", "json", "toml"),
                            default="ini", help="per-model manifest format")
        parser.add_argument("--texture-budget", type=int, default=TEXTURE_BUDGET,
                            help=f"max textures per part (default {TEXTURE_BUDGET})")
        parser.add_argument("--max-texture-size", type=int, metavar="N",
                            help="downscale staged textures larger than N (8-bit)")
        parser.add_argument("--pack-textures", action="store_true",
                            help="pack eligible textures into 512x512 atlases")
        parser.add_argument("--no-pack-texture", action="append", default=[],
                            metavar="GLOB", help="keep matching textures unpacked")
        parser.add_argument("--config", type=Path, metavar="TOML",
                            help="TOML file supplying defaults (explicit CLI wins)")
        parser.add_argument("--no-verify", action="store_true",
                            help="skip the post-merge verification gate")
        parser.add_argument("--dry-run", action="store_true",
                            help="discover and group only; print the plan")

    def run(self, args: argparse.Namespace) -> int:  # noqa: C901 - orchestration
        if args.config is not None:
            _apply_config(args)
        globs = tuple(args.placeholder_seq) or DEFAULT_PLACEHOLDER_GLOBS
        labels = _load_labels(args.labels)

        try:
            donor = load_donor(args.base)
        except MergeViewError as exc:
            print(f"error: donor: {exc}")
            return EXIT_DISCOVERY
        try:
            model_dirs = discover_models(args.models_dir, exclude=set(args.exclude))
        except MergeViewError as exc:
            print(f"error: {exc}")
            return EXIT_DISCOVERY

        models: list[PlayerModel] = []
        failures: list[str] = []
        inventory: list[dict[str, object]] = []
        for model_dir in model_dirs:
            sanitize_model_dir(model_dir)
            try:
                model = load_player_body(model_dir)
            except (MergeViewError, ValueError) as exc:
                failures.append(str(exc))
                print(f"  {model_dir.name:<24} FAIL  {exc}")
                continue
            models.append(model)
            inventory.append({"name": model.name, "hitbox_sig": model.hitbox_sig,
                              "height": model.height, "warnings": model.warnings})

        print(f"  {'-' * 62}")
        print(f"  {len(models)} models loaded, {len(failures)} failed; "
              f"donor {args.base.name} ({len(donor.table)} bones)")
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "inventory.json").write_text(
            json.dumps({"models": inventory, "failures": failures}, indent=1)
        )
        if not models:
            return EXIT_FAIL

        groups = group_models(models, mode=args.group_by,
                              proportion_tolerance=args.proportion_tolerance,
                              labels=labels)
        print(f"  group-by {args.group_by}: {len(groups)} group(s)")
        for key, members in groups:
            parts = split_parts(members, submodel_limit=args.submodel_limit,
                                texture_budget=args.texture_budget,
                                max_skins=args.max_skins,
                                reserve_submodels=1 if args.include_base else 0)
            suffix = f" -> {len(parts)} parts" if len(parts) > 1 else ""
            print(f"    {key:<20} {len(members)} skins{suffix}: "
                  f"{', '.join(m.name for m in members[:6])}"
                  f"{'...' if len(members) > 6 else ''}")
        if args.dry_run:
            return EXIT_FAIL if failures else EXIT_OK

        textures = TextureOptions(max_size=args.max_texture_size,
                                  pack=args.pack_textures, no_pack=args.no_pack_texture)
        aggregate: dict[str, dict[str, object]] = {}
        for key, members in groups:
            gslug = _slug(key)
            parts = split_parts(members, submodel_limit=args.submodel_limit,
                                texture_budget=args.texture_budget,
                                max_skins=args.max_skins,
                                reserve_submodels=1 if args.include_base else 0)
            multi = len(parts) > 1
            for pnum, part in enumerate(parts, 1):
                stem = f"{args.name}_{gslug}" + (f"_p{pnum}" if multi else "")
                part_out = args.out / gslug / (f"p{pnum}" if multi else "")
                include_base = args.include_base and pnum == 1
                try:
                    report = merge_players_part(
                        part, donor, part_out, stem,
                        include_base=include_base, placeholder_globs=globs,
                        submodel_limit=args.submodel_limit, textures=textures,
                        manifest_format=args.manifest_format, write_manifest=True,
                    )
                except MergeError as exc:
                    print(f"error: merge failed ({stem}): {exc}")
                    return EXIT_FAIL
                print(f"    {stem}: {len(part)} skins bones={report.bones} "
                      f"seqs={report.sequences} (voided {report.sequences_deduped}) "
                      f"textures={report.textures}")
                for warning in report.warnings:
                    print(f"      warn: {warning}")
                for skin, body in report.manifest.items():
                    aggregate[f"{gslug}/{skin}"] = {"model": f"{stem}.mdl", **body}

                if not args.no_verify:
                    skins = ([("base", donor.directory, [donor.body_stem])]
                             if include_base else []) + \
                            [(m.name, m.directory, m.body_stems) for m in part]
                    gate = verify_players_part(
                        part_out, f"{stem}.qc", donor, skins,
                        placeholder_globs=globs, submodel_limit=args.submodel_limit,
                        texture_count=report.textures,
                    )
                    for row in gate:
                        mark = "PASS" if row.passed else "FAIL"
                        print(f"      verify {row.check:<20} {mark}  {row.detail}")
                    if not all(row.passed for row in gate):
                        failures.append(f"{stem}: verification gate failed")

        write_manifest_data(args.out, aggregate, args.manifest_format)
        return EXIT_FAIL if failures else EXIT_OK


def _load_labels(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    import tomllib
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    return {str(k): str(v) for k, v in data.items()}


_CONFIG_DEFAULTS: dict[str, object] = {
    "name": "players", "group_by": "size", "proportion_tolerance": 2.0,
    "placeholder_seq": [], "include_base": False, "max_skins": None,
    "submodel_limit": DEFAULT_SUBMODEL_LIMIT, "exclude": [],
    "manifest_format": "ini", "texture_budget": TEXTURE_BUDGET,
    "max_texture_size": None, "pack_textures": False, "no_pack_texture": [],
    "no_verify": False,
}


def _apply_config(args: argparse.Namespace) -> None:
    """Fill flags from a TOML config; explicit CLI values keep priority."""
    import tomllib
    with open(args.config, "rb") as handle:
        config = tomllib.load(handle)
    for key, value in config.items():
        attr = key.replace("-", "_")
        if attr not in _CONFIG_DEFAULTS:
            raise SystemExit(f"error: unknown config key {key!r}")
        if getattr(args, attr) == _CONFIG_DEFAULTS[attr]:
            setattr(args, attr, value)


__all__ = ["MergePlayersCommand"]
