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
from valve_qc_merger.merge_view.discovery import (
    MergeViewError,
    discover_models,
    load_model,
    sanitize_model_dir,
)

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
        parser.add_argument("--dry-run", action="store_true",
                            help="discover, sanitise and load only; print the inventory")

    def run(self, args: argparse.Namespace) -> int:
        try:
            model_dirs = discover_models(args.models_dir, exclude=set(args.exclude))
        except MergeViewError as exc:
            print(f"error: {exc}")
            return EXIT_DISCOVERY

        inventory: list[dict[str, object]] = []
        failures: list[str] = []
        for model_dir in model_dirs:
            renames = sanitize_model_dir(model_dir)
            try:
                model = load_model(model_dir)
            except MergeViewError as exc:
                failures.append(str(exc))
                print(f"  {model_dir.name:<20} FAIL  {exc}")
                continue
            entry = {
                "name": model.name,
                "bones": len(model.bone_names),
                "meshes": len(model.meshes),
                "sequences": len(model.anims),
                "sanitised": renames,
                "warnings": model.warnings,
            }
            inventory.append(entry)
            warn = f"  ({len(model.warnings)} warnings)" if model.warnings else ""
            san = f"  ({len(renames)} files sanitised)" if renames else ""
            print(f"  {model.name:<20} OK    bones={entry['bones']:<4} "
                  f"meshes={entry['meshes']:<2} sequences={entry['sequences']}{san}{warn}")

        print(f"  {'-' * 60}")
        print(f"  {len(inventory)} models loaded, {len(failures)} failed")

        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "inventory.json").write_text(
            json.dumps({"models": inventory, "failures": failures}, indent=1)
        )
        if not args.dry_run:
            print("note: merge stages beyond inventory are not implemented yet "
                  "(spec milestones M2-M5); run with --dry-run to silence this")
        return EXIT_FAIL if failures else EXIT_OK


__all__ = ["MergeViewCommand"]
