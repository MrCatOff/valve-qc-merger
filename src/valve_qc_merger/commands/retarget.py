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

# Stage-1 hand-compatibility gate. Native CSO-2009 hands (ours — male/female)
# measure 1:1 against the reference; a foreign hand mesh is meaningfully
# shorter (deagle 0.857, elite 0.828, anaconda's girl hand 0.926). Anything
# inside this band is "our hands" and takes a straight mesh swap.
_COMPAT_TOLERANCE = 0.01
_INCOMPATIBLE_MESSAGE = (
    "Hand conversion is currently not possible for this type of model"
)


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
        parser.add_argument("--category", default="uncategorized",
                            help="destination bucket under storage/retarget/ "
                                 "(default: uncategorized); the model lands in "
                                 "storage/retarget/{category}/{model}")
        parser.add_argument("--anims",
                            help="glob (relative to weapon-dir) selecting animation SMDs; "
                                 "default: the paths listed by the weapon's QC")
        parser.add_argument("--out", type=Path,
                            help="output directory; overrides the default "
                                 "storage/retarget/{category}/{model} location")
        parser.add_argument("--force", action="store_true",
                            help="run the geometric retarget even when the model's "
                                 "hands are not ours (bypasses the stage-1 "
                                 "compatibility gate; the shape conversion is WIP)")
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

        try:
            out_dir = _resolve_out_dir(args)
        except DriverError as exc:
            print(f"error: {exc}")
            return EXIT_DISCOVERY

        # Stage-1 hand-compatibility split: a model whose bundled hands ARE
        # ours (finger chains 1:1 with the reference) takes a straight
        # male/female mesh swap with its animation intact; a model with foreign
        # hands needs the shape conversion that is not available yet (stage 2),
        # so it is reported and skipped. --force bypasses the gate and runs the
        # geometric offset retarget regardless.
        if args.force:
            config = _resolve_hand_offset(inputs, config)
        else:
            compatible, detail = _hands_compatible(inputs)
            print(detail)
            if not compatible:
                print(_INCOMPATIBLE_MESSAGE)
                return EXIT_OK
            # Native hands are 1:1 with ours, so the direction transfer already
            # reproduces the authored grip angles exactly. Suppress the size-
            # mismatch compensations meant for foreign hands: the auto wrist
            # offset (which would shift both hands ~0.7u off the weapon) and the
            # tip-solve curl (which the residual drift would then trigger). Any
            # value the user set in --config still wins.
            config = dataclasses.replace(
                config,
                hand_offset=config.hand_offset or (0.0, 0.0, 0.0),
                grip_tip_solve=(False if config.grip_tip_solve is None
                                else config.grip_tip_solve),
                grip_sides=config.grip_sides or (),
            )

        try:
            assert_identical_node_tables(inputs)  # §5 gate
            assert_variant_skeletons(inputs)  # hand variants share the reference rig
            blender = find_blender(config)  # environment
        except DriverError as exc:
            print(f"error: {exc}")
            return EXIT_ENV

        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"  output: {out_dir}")
        do_export = not args.dry_run and not args.no_export
        weapon_stem = inputs.weapon_pv.stem
        results: list[SequenceResult] = []
        for index, name in enumerate(inputs.sequences):
            result = run_sequence(
                blender, inputs, name, config, out_dir,
                dry_run=args.dry_run, export=do_export,
                export_mesh=do_export and index == 0, weapon_stem=weapon_stem,
            )
            results.append(result)
            _print_result(result)

        verify = None
        verify_ok = True
        if do_export and any(r.ok for r in results):
            verify, qc_out = finalize_export(inputs, out_dir, results, config)
            verify_ok = _print_verify(verify, qc_out)

        _write_summary(out_dir, config, blender, results, verify)
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


def _resolve_out_dir(args: argparse.Namespace) -> Path:
    """The output directory: an explicit ``--out`` wins, else the model lands
    under ``storage/retarget/{category}/{model}`` (model = weapon-dir name)."""
    if args.out is not None:
        return args.out
    category = args.category.strip()
    if not category or "/" in category or "\\" in category or category in {".", ".."}:
        raise DriverError(f"invalid --category {args.category!r} (must be a plain name)")
    model = args.weapon_dir.name or args.weapon_dir.resolve().name
    return resource_path(Path("storage") / "retarget" / category / model)


def _measure_original_hands(inputs: Inputs) -> object | None:
    """HandScale of the model's bundled hands vs the reference, or None when it
    cannot be measured. Arm discovery is restricted to vertex-weighted bones —
    the same weights the pipeline uses; helper stubs under a wrist would
    otherwise read as a sixth finger (the anaconda)."""
    from valve_qc_merger.merge_view.hands import load_reference_rig
    from valve_qc_merger.parsers.smd import parse_smd_file
    from valve_qc_merger.retarget.handscale import measure_hand_scale

    try:
        hands_smd = parse_smd_file(inputs.original_hands)
        name_of = {n.index: n.name for n in hands_smd.nodes}
        weighted = {name_of[v.bone]
                    for t in hands_smd.triangles for v in t.vertices}
        return measure_hand_scale(
            hands_smd,
            load_reference_rig(inputs.reference),
            parse_smd_file(inputs.reference),
            include=weighted or None,
        )
    except Exception:  # noqa: BLE001 - unmeasurable => treated as incompatible
        return None


def _hands_compatible(inputs: Inputs) -> tuple[bool, str]:
    """Stage-1 split. Returns (compatible, human-readable detail). Compatible
    means the model's bundled hands ARE ours (finger-chain length 1:1 with the
    reference), so replacing them with male/female preserves the animation. A
    size mismatch (foreign hands) needs the shape conversion of stage 2."""
    scale = _measure_original_hands(inputs)
    if scale is None or not scale.chains:  # type: ignore[attr-defined]
        return False, "  hand scale: the model's hand rig could not be matched to the reference"
    ratio = scale.ratio  # type: ignore[attr-defined]
    if abs(ratio - 1.0) < _COMPAT_TOLERANCE:
        return True, (f"  hand scale: {ratio:.3f}x vs reference — native hands; "
                      "swapping to male/female")
    return False, (f"  hand scale: {ratio:.3f}x vs reference "
                   f"(chain surplus {scale.surplus:+.2f}u) — foreign hands"  # type: ignore[attr-defined]
                   )


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


def _resolve_hand_offset(inputs: Inputs, config: RetargetConfig) -> RetargetConfig:
    """Measure hand size vs the reference; derive the pair-calibrated offset.

    Separates the two coverage-failure classes up front: a ratio near 1.0
    means any grip problem is pose/rig, NOT size. A real mismatch derives the
    full 3-D hand offset (palm-forward AND lateral, GUN_SHIFT_PER_SURPLUS
    calibrated on the v_deagle/v_g_deagle authored pair) from the source's
    idle grip pose and injects it as the explicit ``hand_offset`` — printed,
    and skipped entirely when the config already sets one. Never blocks the
    run — measurement failures fall back to the worker's palm-forward auto.
    """
    try:
        from valve_qc_merger.parsers.smd import parse_smd_file
        from valve_qc_merger.retarget.handscale import (
            auto_hand_offsets,
            dominant_weapon_bone,
            grip_sides,
        )

        scale = _measure_original_hands(inputs)
        if scale is None or not scale.chains:
            print("  hand scale: no complete finger chains matched")
            return config
        if abs(scale.ratio - 1.0) < 0.01:
            print(f"  hand scale: {scale.ratio:.3f}x vs reference — hands "
                  "match; grip issues here are pose/rig, not size")
            return config
        if config.hand_offset is not None:
            print(f"  hand scale: {scale.ratio:.3f}x vs reference (chain "
                  f"surplus {scale.surplus:+.2f}u); config hand_offset "
                  "overrides the calibrated compensation")
            return config
        offsets: dict[str, object] = dict(auto_hand_offsets(scale))
        if not offsets:
            print(f"  hand scale: {scale.ratio:.3f}x vs reference (chain "
                  f"surplus {scale.surplus:+.2f}u); grip bones unmatched — "
                  "falling back to the palm-forward worker offset")
            return config
        # Gripping side(s): the wrist the weapon follows rigidly across the
        # model's own animations (CS viewmodels are authored left-handed, so
        # this is usually the LEFT hand — the support hand's fingers sit even
        # closer to the gun, so only rigidity tells them apart).
        sides: list[str] = []
        if config.grip_sides is not None:
            sides = list(config.grip_sides)
        else:
            weapon_smd = parse_smd_file(inputs.weapon_pv)
            weapon_bone = dominant_weapon_bone(weapon_smd)
            # Probe EVERY sequence: only the dynamic ones (draw, reload)
            # separate the gripping wrist from the support wrist — during
            # idle/shoot both hands hold still on the gun.
            probe_anims = [
                parse_smd_file(path) for path in inputs.sequences.values()
            ]
            sides = grip_sides(scale, probe_anims, weapon_bone)
        rendered = {s: (round(v.x, 2), round(v.y, 2), round(v.z, 2))  # type: ignore[attr-defined]
                    for s, v in offsets.items()}
        print(f"  hand scale: {scale.ratio:.3f}x vs reference (chain surplus "
              f"{scale.surplus:+.2f}u over {scale.chains} chains); "
              f"per-side offsets {rendered} (authored-median x surplus); "
              f"gripping side(s): {sides or 'none detected'}")
        return dataclasses.replace(
            config,
            hand_offsets_by_side={
                s: (v.x, v.y, v.z)  # type: ignore[attr-defined]
                for s, v in offsets.items()
            },
            grip_sides=tuple(sides),
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        print(f"  hand scale: unmeasured ({exc})")
        return config


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
