"""CLI wiring for merge-player.

Merges a folder of decompiled p_ (player-held) weapon models into as few
compilable .mdl files as studiomdl's hard limits allow. The engine bone-merges
the shared ``Bip01`` chain by name against the player model at runtime, so a
part carries that chain once, ONE uniquely-named bone per held object (two for
dual-wield), one geometry submodel per weapon behind a single ``weapons``
bodygroup (``pev_body 0`` = blank), and a single-frame ``idle`` sequence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.merge_player.analyze import (
    PlayerAnalyzeError,
    PlayerPlan,
    collapse_weapon_bones,
)
from valve_qc_merger.merge_player.merger import (
    merge_player_models,
    skin_texture_files,
)
from valve_qc_merger.merge_player.parts import (
    TEXTURE_BUDGET,
    PlayerBudget,
    split_player_parts,
)
from valve_qc_merger.merge_player.verify import verify_player_part
from valve_qc_merger.merge_view.atlas import TextureOptions
from valve_qc_merger.merge_view.discovery import (
    MergeViewError,
    ModelInput,
    discover_models,
    load_model,
    sanitize_model_dir,
)
from valve_qc_merger.merge_view.merger import MergeError, write_manifest_data
from valve_qc_merger.parsers.smd import parse_smd_file

EXIT_OK = 0
EXIT_FAIL = 2
EXIT_DISCOVERY = 3


def _load_player_model(model_dir: Path) -> ModelInput:
    """Load a p_ model; QCs without ``$sequence`` fall back to an on-disk idle.

    Some decompiles (p_tknife) ship an anims folder the QC never references;
    an animation matching the model's own skeleton is still the best source
    for the weapon bone's in-game pose, so adopt the first one that parses.
    """
    model = load_model(model_dir, require_anims=False)
    if not model.anims:
        bones = set(model.bone_names)
        for candidate in sorted(model_dir.glob("*/*.smd")):
            try:
                smd = parse_smd_file(candidate)
            except (OSError, ValueError):
                continue
            if smd.frames and {n.name for n in smd.nodes} <= bones:
                model.anims[candidate.stem] = smd
                model.warnings.append(
                    f"QC has no $sequence; using {candidate.name} for the "
                    "idle pose"
                )
                break
    return model


class MergePlayerCommand(Command):
    """Merge decompiled p_ weapon models into combined bodygrouped models."""

    name = "merge-player"
    help = "merge a folder of decompiled p_ (player) weapon models into combined models"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("models_dir", type=Path,
                            help="parent directory; each subdir with one .qc is a model")
        parser.add_argument("--out", type=Path, required=True, help="output directory")
        parser.add_argument("--name", default="p_merged", help="output model name stem")
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
        pairs: list[tuple[ModelInput, PlayerPlan]] = []
        skin_textures: dict[str, set[str]] = {}
        for model_dir in model_dirs:
            sanitised = sanitize_model_dir(model_dir)
            try:
                model = _load_player_model(model_dir)
                plan = collapse_weapon_bones(model)
            except (MergeViewError, PlayerAnalyzeError, ValueError) as exc:
                failures.append(str(exc))
                print(f"  {model_dir.name:<20} FAIL  {exc}")
                continue
            pairs.append((model, plan))
            skin_textures[model.name] = skin_texture_files(model.qc_text)
            entry: dict[str, object] = {
                "name": model.name,
                "shared_bones": len(plan.shared),
                "weapon_bones": [
                    {"bone": b.final, "from": b.original, "anchor": b.anchor,
                     "collapsed": list(b.removed)}
                    for b in plan.bones
                ],
                "sequences": len(model.anims),
                "sanitised": sanitised,
                "warnings": model.warnings + plan.warnings,
            }
            inventory.append(entry)
            hands = "+".join(
                "L" if " l " in f" {b.anchor.lower()} " else "R"
                for b in plan.bones
            ) or "shared-only"
            warn = (f"  ({len(entry['warnings'])} warnings)"  # type: ignore[arg-type]
                    if entry["warnings"] else "")
            print(f"  {model.name:<20} OK    bones=+{len(plan.bones)} "
                  f"({hands}) sequences={len(model.anims)}{warn}")

        print(f"  {'-' * 60}")
        print(f"  {len(pairs)} models loaded, {len(failures)} failed")
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "inventory.json").write_text(
            json.dumps({"models": inventory, "failures": failures}, indent=1)
        )
        if args.dry_run or not pairs:
            return EXIT_FAIL if failures else EXIT_OK

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
            part_name = f"{args.name}_p{number}" if multi else args.name
            part_out = args.out / f"p{number}" if multi else args.out
            try:
                report = merge_player_models(
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
            print(f"  {part_name}: {len(part_pairs)} weapons "
                  f"bones={report.bones} textures={report.textures}")
            for warning in report.warnings:
                print(f"    warn: {warning}")
            for model, _plan in part_pairs:
                aggregate[model.name] = {
                    "model": f"{part_name}.mdl",
                    **report.manifest[model.name],
                }
            if not args.no_verify:
                bone_maps = {
                    model.name: plan.bone_map for model, plan in part_pairs
                }
                anchors = {
                    model.name: {b.final: b.anchor for b in plan.bones}
                    for model, plan in part_pairs
                }
                gate = verify_player_part(
                    part_out, f"{part_name}.qc", args.models_dir,
                    [m.name for m, _ in part_pairs], bone_maps, anchors,
                )
                for row in gate:
                    mark = "PASS" if row.passed else "FAIL"
                    print(f"    verify {row.check:<22} {mark}  {row.detail}")
                if not all(row.passed for row in gate):
                    failures.append(f"{part_name}: verification gate failed")
        if multi:
            write_manifest_data(args.out, aggregate, args.manifest_format)
        return EXIT_FAIL if failures else EXIT_OK


_CONFIG_DEFAULTS: dict[str, object] = {
    "name": "p_merged",
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


__all__ = ["MergePlayerCommand"]
