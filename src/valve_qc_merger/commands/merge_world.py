"""CLI wiring for merge-w.

Merges a folder of decompiled w_ (dropped-weapon) models into as few
compilable .mdl files as studiomdl's hard limits allow. A w_ model renders at
its entity origin, so the merged part carries just TWO bones — a ``flash``
root and a ``weapon`` bone holding every vertex, giving one auto-generated
hitbox that always covers the visible weapon. Each model's rendered pose
(idle over bind) is baked into its vertices first, so everything sits exactly
where the original .mdl drew it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.commands.merge_player import _load_player_model
from valve_qc_merger.merge_player.analyze import PlayerPlan
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

EXIT_OK = 0
EXIT_FAIL = 2
EXIT_DISCOVERY = 3


class MergeWorldCommand(Command):
    """Merge decompiled w_ weapon models into combined bodygrouped models."""

    name = "merge-w"
    help = "merge a folder of decompiled w_ (world) weapon models into combined models"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("models_dir", type=Path,
                            help="parent directory; each subdir with one .qc is a model")
        parser.add_argument("--out", type=Path, required=True, help="output directory")
        parser.add_argument("--name", default="w_merged", help="output model name stem")
        parser.add_argument("--exclude", action="append", default=[],
                            metavar="NAME", help="skip a model directory (repeatable)")
        parser.add_argument("--manifest-format", choices=("ini", "json", "toml"),
                            default="ini", help="per-model manifest format")
        parser.add_argument("--texture-budget", type=int, default=TEXTURE_BUDGET,
                            help="max textures per compiled part (default "
                                 f"{TEXTURE_BUDGET}; hard engine cap is 100)")
        parser.add_argument("--max-texture-size", type=int, metavar="N",
                            help="downscale staged textures larger than N on "
                                 "either axis (8-bit re-quantised)")
        parser.add_argument("--pack-textures", action="store_true",
                            help="pack eligible textures four-to-a-file into "
                                 "512x512 atlases (skin-family textures stay "
                                 "standalone)")
        parser.add_argument("--no-pack-texture", action="append", default=[],
                            metavar="GLOB",
                            help="keep matching textures out of atlases "
                                 "(repeatable)")
        parser.add_argument("--config", type=Path, metavar="TOML",
                            help="TOML file supplying defaults for any flag "
                                 "(explicit CLI values win)")
        parser.add_argument("--no-verify", action="store_true",
                            help="skip the post-merge verification gate")
        parser.add_argument("--dry-run", action="store_true",
                            help="discover, sanitise and analyse only; print "
                                 "the inventory")

    def run(self, args: argparse.Namespace) -> int:
        if args.config is not None:
            _apply_config(args)
        try:
            model_dirs = discover_models(args.models_dir, exclude=set(args.exclude))
        except MergeViewError as exc:
            print(f"error: {exc}")
            return EXIT_DISCOVERY

        inventory: list[dict[str, object]] = []
        failures: list[str] = []
        loaded: list[ModelInput] = []
        skin_textures: dict[str, set[str]] = {}
        for model_dir in model_dirs:
            sanitised = sanitize_model_dir(model_dir)
            try:
                model = _load_player_model(model_dir)
                plan = bake_rendered_pose(model)
            except (MergeViewError, ValueError) as exc:
                failures.append(str(exc))
                print(f"  {model_dir.name:<20} FAIL  {exc}")
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
            print(f"  {model.name:<20} OK    bones={len(model.bone_names)}"
                  f"{bake}{warn}")

        print(f"  {'-' * 60}")
        print(f"  {len(loaded)} models loaded, {len(failures)} failed")
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "inventory.json").write_text(
            json.dumps({"models": inventory, "failures": failures}, indent=1)
        )
        if args.dry_run or not loaded:
            return EXIT_FAIL if failures else EXIT_OK

        # Reuse the player splitter: every model costs the same two bones.
        pairs = [
            (model, PlayerPlan(model=model.name,
                               shared=[FLASH_BONE, WEAPON_BONE]))
            for model in loaded
        ]
        parts = split_player_parts(
            pairs,
            PlayerBudget(textures=args.texture_budget),
            skin_textures=skin_textures,
        )
        multi = len(parts) > 1
        if multi:
            print(f"  split: {len(parts)} parts "
                  f"(studiomdl caps one model at 32 submodels)")
        aggregate: dict[str, dict[str, object]] = {}
        for number, part_pairs in enumerate(parts, 1):
            part_models = [model for model, _plan in part_pairs]
            part_name = f"{args.name}_p{number}" if multi else args.name
            part_out = args.out / f"p{number}" if multi else args.out
            try:
                report = merge_world_models(
                    part_models, part_out, part_name,
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
            print(f"  {part_name}: {len(part_models)} weapons "
                  f"bones={report.bones} textures={report.textures}")
            for warning in report.warnings:
                print(f"    warn: {warning}")
            for model in part_models:
                aggregate[model.name] = {
                    "model": f"{part_name}.mdl",
                    **report.manifest[model.name],
                }
            if report.atlas:
                aggregate[f"textures_{part_name}"] = dict(report.atlas)
            if not args.no_verify:
                gate = verify_world_part(
                    part_out, f"{part_name}.qc", args.models_dir,
                    [m.name for m in part_models],
                )
                for row in gate:
                    mark = "PASS" if row.passed else "FAIL"
                    print(f"    verify {row.check:<20} {mark}  {row.detail}")
                if not all(row.passed for row in gate):
                    failures.append(f"{part_name}: verification gate failed")
        if multi:
            write_manifest_data(args.out, aggregate, args.manifest_format)
        return EXIT_FAIL if failures else EXIT_OK


_CONFIG_DEFAULTS: dict[str, object] = {
    "name": "w_merged",
    "exclude": [],
    "manifest_format": "ini",
    "texture_budget": TEXTURE_BUDGET,
    "max_texture_size": None,
    "pack_textures": False,
    "no_pack_texture": [],
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


__all__ = ["MergeWorldCommand"]
