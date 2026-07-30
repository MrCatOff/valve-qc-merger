"""CLI wiring for the Blender-driven retargeting pipeline (§9).

Thin argument parsing over :mod:`valve_qc_merger.retarget.driver`. The heavy
lifting happens in headless Blender workers; this command resolves inputs, runs
the §5 text gate, launches the workers and maps their outcomes to exit codes.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.retarget.config import RetargetConfig
from valve_qc_merger.retarget.driver import (
    DriverError,
    SequenceResult,
    assert_identical_node_tables,
    find_blender,
    resolve_inputs,
    run_sequence,
)

# Exit codes (§9).
EXIT_OK = 0
EXIT_DEGRADED = 1
EXIT_FAIL = 2
EXIT_DISCOVERY = 3
EXIT_ENV = 4


class RetargetCommand(Command):
    """Retarget a weapon's animations onto the reference hands."""

    name = "retarget"
    help = "retarget a weapon's animations onto the reference hands (Blender pipeline)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--reference", type=Path, required=True,
                            help="reference hands SMD (immutable)")
        parser.add_argument("--weapon-dir", type=Path, required=True,
                            help="weapon directory (holds *-PV.smd, hand mesh, anims)")
        parser.add_argument("--anims", default="v_elite_anims/*.smd",
                            help="glob (relative to weapon-dir) selecting animation SMDs")
        parser.add_argument("--out", type=Path, required=True, help="output directory")
        parser.add_argument("--config", type=Path, help="TOML config; omitted keys take defaults")
        parser.add_argument("--weapon-pv", type=Path, help="override the *-PV.smd weapon mesh")
        parser.add_argument("--original-hands", type=Path, help="override the original hand mesh")
        parser.add_argument("--sequences", help="comma-separated subset of sequence names")
        parser.add_argument("--jobs", type=int, default=1, help="parallel workers (reserved)")
        parser.add_argument("--blender", help="Blender executable (else autodiscovered)")
        parser.add_argument("--dry-run", action="store_true",
                            help="import + discovery + correspondence only; no solve/export")

    def run(self, args: argparse.Namespace) -> int:
        try:
            config = _load_config(args)
            inputs = resolve_inputs(
                args.reference, args.weapon_dir, args.anims,
                weapon_pv=args.weapon_pv, original_hands=args.original_hands,
                only=_selected(args.sequences),
            )
            assert_identical_node_tables(inputs)
            blender = find_blender(config)
        except DriverError as exc:
            print(f"error: {exc}")
            return EXIT_DISCOVERY

        args.out.mkdir(parents=True, exist_ok=True)
        results: list[SequenceResult] = []
        for name in inputs.sequences:
            result = run_sequence(blender, inputs, name, config, args.out)
            results.append(result)
            _print_result(result)

        _write_summary(args.out, config, blender, results)
        return _exit_code(results)


def _load_config(args: argparse.Namespace) -> RetargetConfig:
    config = RetargetConfig.from_toml(args.config) if args.config else RetargetConfig()
    if args.blender:
        config = dataclasses.replace(config, blender=args.blender)
    return config


def _selected(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    return {name.strip() for name in raw.split(",") if name.strip()}


def _print_result(result: SequenceResult) -> None:
    status = result.report.get("status", "?")
    if result.ok:
        counts = result.report.get("counts", {})
        print(f"  {result.name:<16} {status}  {counts}")
    else:
        error = result.report.get("error", "worker crashed")
        print(f"  {result.name:<16} {status}  {error}")


def _write_summary(
    out_dir: Path, config: RetargetConfig, blender: str, results: list[SequenceResult]
) -> None:
    summary = {
        "blender": blender,
        "config": config.to_job_dict(),
        "sequences": {r.name: {"exit_code": r.exit_code, "report": r.report} for r in results},
    }
    (out_dir / "report.json").write_text(json.dumps(summary, indent=2))


def _exit_code(results: list[SequenceResult]) -> int:
    if not results:
        return EXIT_FAIL
    codes = {r.exit_code for r in results}
    if codes == {EXIT_OK}:
        return EXIT_OK
    if EXIT_ENV in codes:
        return EXIT_ENV
    if EXIT_DISCOVERY in codes:
        return EXIT_DISCOVERY
    return EXIT_FAIL


__all__ = ["RetargetCommand"]
