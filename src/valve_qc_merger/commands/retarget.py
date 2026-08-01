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
from valve_qc_merger.resources import resource_path
from valve_qc_merger.retarget.config import RetargetConfig
from valve_qc_merger.retarget.driver import (
    DriverError,
    Inputs,
    SequenceResult,
    assert_identical_node_tables,
    assert_variant_skeletons,
    finalize_export,
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
        parser.add_argument("--reference", type=Path,
                            help="reference hands SMD (immutable); default "
                                 "storage/hands/reference_hands.smd or config")
        parser.add_argument("--weapon-dir", type=Path, required=True,
                            help="weapon directory (holds *-PV.smd, hand mesh, anims)")
        parser.add_argument("--anims",
                            help="glob (relative to weapon-dir) selecting animation SMDs; "
                                 "default: the paths listed by the weapon's QC")
        parser.add_argument("--out", type=Path, required=True, help="output directory")
        parser.add_argument("--config", type=Path, help="TOML config; omitted keys take defaults")
        parser.add_argument("--weapon-pv", type=Path, help="override the *-PV.smd weapon mesh")
        parser.add_argument("--original-hands", type=Path, help="override the original hand mesh")
        parser.add_argument("--sequences", help="comma-separated subset of sequence names")
        parser.add_argument("--jobs", type=int, default=1, help="parallel workers (reserved)")
        parser.add_argument("--blender", help="Blender executable (else autodiscovered)")
        parser.add_argument("--dry-run", action="store_true",
                            help="import + discovery + correspondence only; no solve/export")
        parser.add_argument("--no-export", action="store_true",
                            help="retarget only; do not unify the skeleton or write SMDs")
        parser.add_argument("--hands", action="append", metavar="NAME=PATH",
                            help="hand mesh variant for $bodygroup output (repeatable); "
                                 "each must share the reference skeleton")

    def run(self, args: argparse.Namespace) -> int:
        try:
            config = _load_config(args)
            reference = resource_path(args.reference or Path(config.reference))
            if not reference.exists():
                raise DriverError(f"reference hands SMD not found: {reference}")
            inputs = resolve_inputs(
                reference, args.weapon_dir, args.anims,
                weapon_pv=args.weapon_pv, original_hands=args.original_hands,
                only=_selected(args.sequences),
                hand_variants=config.resolved_hand_variants(),
            )
        except DriverError as exc:
            print(f"error: {exc}")
            return EXIT_DISCOVERY  # bad/missing inputs
        _print_hand_scale(inputs, config)
        try:
            assert_identical_node_tables(inputs)  # §5 gate
            assert_variant_skeletons(inputs)  # hand variants share the reference rig
            blender = find_blender(config)  # environment
        except DriverError as exc:
            print(f"error: {exc}")
            return EXIT_ENV

        args.out.mkdir(parents=True, exist_ok=True)
        do_export = not args.dry_run and not args.no_export
        weapon_stem = inputs.weapon_pv.stem
        results: list[SequenceResult] = []
        for index, name in enumerate(inputs.sequences):
            result = run_sequence(
                blender, inputs, name, config, args.out,
                dry_run=args.dry_run, export=do_export,
                export_mesh=do_export and index == 0, weapon_stem=weapon_stem,
            )
            results.append(result)
            _print_result(result)

        verify = None
        verify_ok = True
        if do_export and any(r.ok for r in results):
            verify, qc_out = finalize_export(inputs, args.out, results, config)
            verify_ok = _print_verify(verify, qc_out)

        _write_summary(args.out, config, blender, results, verify)
        code = _exit_code(results)
        if code == EXIT_OK and not verify_ok:
            return EXIT_FAIL
        return code


def _load_config(args: argparse.Namespace) -> RetargetConfig:
    config = RetargetConfig.from_toml(args.config) if args.config else RetargetConfig()
    if args.blender:
        config = dataclasses.replace(config, blender=args.blender)
    if args.hands:
        variants = config.resolved_hand_variants()
        for spec in args.hands:
            if spec == "blank":
                variants = {}
                continue
            name, sep, path = spec.partition("=")
            if not sep or not name or not path:
                raise DriverError(f"--hands expects NAME=PATH (or 'blank'), got {spec!r}")
            variants[name] = path
        config = dataclasses.replace(config, hand_variants=variants)
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


def _print_verify(verify: object, qc_out: object) -> bool:
    """Print the Phase 6 gate outcome; return True if it passed (or was skipped)."""
    from valve_qc_merger.retarget.verify_smd import VerifyResult

    if not isinstance(verify, VerifyResult):
        print("  verify           SKIPPED (no exported model found)")
        return True
    status = "PASS" if verify.ok else "FAIL"
    passed = sum(1 for v in verify.checks.values() if v)
    print(f"  verify           {status}  ({passed}/{len(verify.checks)} checks)")
    for warning in verify.warnings:
        print(f"    warn: {warning}")
    for error in verify.errors:
        print(f"    error: {error}")
    if qc_out is not None:
        print(f"  qc               {qc_out}")
    return verify.ok


def _write_summary(
    out_dir: Path, config: RetargetConfig, blender: str, results: list[SequenceResult],
    verify: object = None,
) -> None:
    from valve_qc_merger.retarget.verify_smd import VerifyResult

    summary: dict[str, object] = {
        "blender": blender,
        "config": config.to_job_dict(),
        "sequences": {r.name: {"exit_code": r.exit_code, "report": r.report} for r in results},
    }
    if isinstance(verify, VerifyResult):
        summary["verify"] = {
            "ok": verify.ok,
            "checks": verify.checks,
            "errors": verify.errors,
            "warnings": verify.warnings,
        }
    (out_dir / "report.json").write_text(json.dumps(summary, indent=2))


def _print_hand_scale(inputs: Inputs, config: RetargetConfig) -> None:
    """Diagnostic: source hand size vs the reference, before anything runs.

    Separates the two coverage-failure classes up front: a ratio near 1.0
    means any grip problem is pose/rig, NOT size (no offset will fix it); a
    real mismatch prints the surplus and the auto compensation the worker
    will apply (still overridable via ``hand_offset`` in the config). Never
    blocks the run — measurement failures just report themselves.
    """
    try:
        from valve_qc_merger.merge_view.hands import load_reference_rig
        from valve_qc_merger.parsers.smd import parse_smd_file
        from valve_qc_merger.retarget.handscale import measure_hand_scale

        hands_smd = parse_smd_file(inputs.original_hands)
        # Restrict arm discovery to vertex-weighted bones — the same weights
        # signal the pipeline uses; helper stubs under a wrist would
        # otherwise read as a sixth finger (anaconda).
        name_of = {n.index: n.name for n in hands_smd.nodes}
        weighted = {name_of[v.bone]
                    for t in hands_smd.triangles for v in t.vertices}
        scale = measure_hand_scale(
            hands_smd,
            load_reference_rig(inputs.reference),
            parse_smd_file(inputs.reference),
            include=weighted or None,
        )
        if not scale.chains:
            print("  hand scale: no complete finger chains matched")
            return
        if abs(scale.ratio - 1.0) < 0.01:
            print(f"  hand scale: {scale.ratio:.3f}x vs reference — hands "
                  "match; grip issues here are pose/rig, not size")
            return
        shift = scale.surplus * config.hand_center_fraction
        override = (" (config hand_offset overrides this)"
                    if config.hand_offset is not None else "")
        print(f"  hand scale: {scale.ratio:.3f}x vs reference (chain surplus "
              f"{scale.surplus:+.2f}u over {scale.chains} chains); auto hand "
              f"offset shifts ~{shift:.2f}u along the palm axis{override}")
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        print(f"  hand scale: unmeasured ({exc})")


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
