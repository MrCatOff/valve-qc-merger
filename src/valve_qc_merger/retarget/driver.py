"""Non-Blender driver for the retargeting pipeline.

Runs in ordinary Python. It resolves inputs, performs the cheap text-level §5
gate (identical node tables across the weapon, original hands and every
animation), then launches one headless Blender worker per sequence and collects
the JSON reports. Post-export verification (Phase 6) parses the emitted SMDs
with :mod:`valve_qc_merger.parsers.smd`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path

from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget.config import RetargetConfig
from valve_qc_merger.retarget.euler_unwrap import unwrap_smd
from valve_qc_merger.retarget.qc_build import (
    QcSequence,
    build_qc,
    modelname,
    parse_bodygroups,
    parse_sequences,
)
from valve_qc_merger.retarget.textures import finalize_textures
from valve_qc_merger.retarget.verify_smd import VerifyResult, verify_export
from valve_qc_merger.writers.smd import write_smd_file

WORKER = Path(__file__).with_name("worker.py")

_MAC_BLENDER = "/Applications/Blender.app/Contents/MacOS/Blender"

# Worker environment/assertion failure (matches worker.EXIT_ENV and the CLI).
EXIT_ENV = 4


class DriverError(RuntimeError):
    """A driver-level failure (bad inputs, missing Blender, §5 gate)."""


def find_blender(config: RetargetConfig) -> str:
    """Locate the Blender executable (config > env > PATH > macOS default)."""
    for candidate in (config.blender, os.environ.get("VQM_BLENDER"), shutil.which("blender")):
        if candidate and Path(candidate).exists():
            return candidate
    if Path(_MAC_BLENDER).exists():
        return _MAC_BLENDER
    raise DriverError(
        "Blender not found. Set --blender, the VQM_BLENDER env var, or put it on PATH."
    )


@dataclass(frozen=True)
class Inputs:
    """Resolved input paths for one weapon."""

    reference: Path
    weapon_pv: Path
    original_hands: Path
    sequences: dict[str, Path]  # name -> anim SMD
    hand_variants: dict[str, Path] = field(default_factory=dict)  # bodygroup name -> SMD
    # Every ``$bodygroup "weapon"`` studio, in QC order (the first is ``weapon_pv``).
    # A weapon whose mesh is split across several always-on bodyparts (bloodhunter
    # = pistol body + blood projectile + effects) lists more than one; each is
    # retargeted onto the same unified skeleton and emitted as its own weapon
    # bodygroup, so every part stays a separate submodel under the 2048-vertex
    # engine cap (merging them into one submodel would overflow it).
    weapon_studios: tuple[Path, ...] = ()


def _one(matches: list[str], what: str, where: Path) -> Path:
    if not matches:
        raise DriverError(f"no {what} found in {where}")
    if len(matches) > 1:
        raise DriverError(f"multiple {what} in {where}: {[Path(m).name for m in matches]}")
    return Path(matches[0])


def _qc_smd(weapon_dir: Path, stem: str) -> Path:
    """Resolve a QC-referenced SMD stem/path (backslashes, optional extension)."""
    relative = stem.replace("\\", "/")
    if not relative.lower().endswith(".smd"):
        relative += ".smd"
    return weapon_dir / relative


def resolve_inputs(
    reference: Path,
    weapon_dir: Path,
    anims_glob: str | None = None,
    *,
    weapon_pv: Path | None = None,
    original_hands: Path | None = None,
    only: set[str] | None = None,
    hand_variants: dict[str, str] | None = None,
) -> Inputs:
    """Discover the weapon mesh, original hands and animation set.

    Everything not given explicitly is read from the weapon's QC — the
    authoritative manifest: ``$bodygroup "weapon"`` names the weapon mesh,
    ``$bodygroup "hands"`` the original hand meshes (the first entry is the
    contact ground truth), and every ``$sequence`` block carries its animation
    SMD path. ``anims_glob`` remains as a filesystem-glob override.
    """
    weapon_dir = weapon_dir.resolve()
    qc_src = _find_qc(weapon_dir)
    bodygroups: dict[str, list[str]] = {}
    qc_sequences: list[QcSequence] = []
    if qc_src is not None:
        qc_text = qc_src.read_text(errors="replace")
        bodygroups = parse_bodygroups(qc_text)
        qc_sequences = parse_sequences(qc_text)

    pv = weapon_pv
    studio_paths: tuple[Path, ...] = ()
    if pv is None:
        # Weapon parts = every bodygroup that is NOT the hands group, in QC order.
        # The weapon group's name varies across the corpus — "weapon", the
        # decompiler's default "studio", even a "waepon" typo — and a weapon split
        # across several always-on blocks (bloodhunter: pistol + projectile +
        # effects; dual-wield: two "studio" blocks) has parse_bodygroups de-dup the
        # repeats to name/name_2/... . Matching everything except hands collects
        # them all (dict preserves QC order); a literal "blank" submodel is skipped.
        weapon_studios = [
            stem
            for key, stems in bodygroups.items()
            if not re.fullmatch(r"hands(_\d+)?", key)
            for stem in stems
            if stem.lower() != "blank"
        ]
        if weapon_studios:
            studio_paths = tuple(_qc_smd(weapon_dir, s) for s in weapon_studios)
            missing = [p for p in studio_paths if not p.exists()]
            if missing:
                raise DriverError(
                    f"weapon mesh not found: {missing[0]}"
                )
            pv = studio_paths[0]
        else:
            pv = _one(sorted(glob(str(weapon_dir / "*-PV.smd"))),
                      "*-PV.smd weapon mesh", weapon_dir)
    if not pv.exists():
        raise DriverError(f"weapon mesh not found: {pv}")

    hands = original_hands
    if hands is None:
        hand_studios = bodygroups.get("hands", [])
        candidates = [
            _qc_smd(weapon_dir, stem) for stem in hand_studios
        ] or [Path(p) for p in sorted(glob(str(weapon_dir / "f_*_hand_Low.smd")))]
        existing = [c for c in candidates if c.exists()]
        if not existing:
            raise DriverError(
                f"original hand mesh not found in {weapon_dir} "
                "(no $bodygroup \"hands\" studio resolves; pass --original-hands)"
            )
        hands = existing[0]

    if anims_glob is not None:
        anim_paths = sorted(glob(str(weapon_dir / anims_glob)))
        if not anim_paths:
            raise DriverError(f"no animations matched {anims_glob!r} under {weapon_dir}")
        sequences = {Path(p).stem: Path(p) for p in anim_paths}
    else:
        sequences = {}
        for seq in qc_sequences:
            if seq.smd is None:
                continue
            path = _qc_smd(weapon_dir, seq.smd)
            if not path.exists():
                raise DriverError(
                    f"QC sequence {seq.name!r} references missing SMD: {path}"
                )
            sequences[seq.name] = path
        if not sequences:
            raise DriverError(
                f"no $sequence entries found in {qc_src or weapon_dir}; "
                "pass --anims with a glob"
            )
    if only is not None:
        missing = only - sequences.keys()
        if missing:
            raise DriverError(f"requested sequences not found: {sorted(missing)}")
        sequences = {name: sequences[name] for name in sorted(only)}
    variants: dict[str, Path] = {}
    for variant_name, raw in sorted((hand_variants or {}).items()):
        path = Path(raw)
        if not path.exists():
            raise DriverError(f"hand variant {variant_name!r} not found: {path}")
        variants[variant_name] = path.resolve()
    return Inputs(reference.resolve(), pv, hands, sequences, variants, studio_paths)


def assert_identical_node_tables(inputs: Inputs) -> list[tuple[int, str, int]]:
    """§5 gate: weapon, original hands and every anim share one node table."""
    reference_table: list[tuple[int, str, int]] | None = None
    reference_src = ""
    to_check = {"weapon": inputs.weapon_pv, "original_hands": inputs.original_hands}
    to_check.update({f"anim:{name}": path for name, path in inputs.sequences.items()})
    for label, path in to_check.items():
        table = [(n.index, n.name, n.parent) for n in parse_smd_file(path).nodes]
        if reference_table is None:
            reference_table, reference_src = table, label
        elif table != reference_table:
            raise DriverError(
                f"node table of {label} ({path.name}) differs from {reference_src}; "
                "the weapon, original hands and animations must share one skeleton (§5)"
            )
    assert reference_table is not None
    return reference_table


def assert_variant_skeletons(inputs: Inputs) -> None:
    """Every hand variant must share the reference skeleton EXACTLY.

    Bodygroup variants swap meshes on one skeleton; a variant with different
    bone names, parents or rest transforms would deform wrongly in-game while
    compiling fine. Checked at text level before any worker runs.
    """
    if not inputs.hand_variants:
        return
    reference = parse_smd_file(inputs.reference)
    ref_table = [(n.index, n.name, n.parent) for n in reference.nodes]
    ref_rest = [
        (p.bone, round(p.position.x, 4), round(p.position.y, 4), round(p.position.z, 4),
         round(p.rotation.x, 4), round(p.rotation.y, 4), round(p.rotation.z, 4))
        for p in reference.frames[0].poses
    ]
    for name, path in inputs.hand_variants.items():
        variant = parse_smd_file(path)
        table = [(n.index, n.name, n.parent) for n in variant.nodes]
        if table != ref_table:
            raise DriverError(
                f"hand variant {name!r} ({path.name}) has a different node table "
                "from the reference hands; bodygroup variants must share one skeleton"
            )
        rest = [
            (p.bone, round(p.position.x, 4), round(p.position.y, 4),
             round(p.position.z, 4), round(p.rotation.x, 4), round(p.rotation.y, 4),
             round(p.rotation.z, 4))
            for p in variant.frames[0].poses
        ]
        if rest != ref_rest:
            raise DriverError(
                f"hand variant {name!r} ({path.name}) has a different rest skeleton "
                "from the reference hands; bodygroup variants must share one skeleton"
            )


def _as_text(stream: str | bytes | None) -> str:
    if stream is None:
        return ""
    return stream.decode(errors="replace") if isinstance(stream, bytes) else stream


@dataclass
class SequenceResult:
    """Outcome of one worker run."""

    name: str
    exit_code: int
    report: dict[str, object] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def run_sequence(
    blender: str,
    inputs: Inputs,
    name: str,
    config: RetargetConfig,
    out_dir: Path,
    *,
    dry_run: bool = False,
    export: bool = False,
    export_mesh: bool = False,
    weapon_stem: str = "model",
    timeout: float = 600.0,
) -> SequenceResult:
    """Launch one headless worker for a single sequence and read its report.

    A crash in the worker *before* its own try/except (bad ``--job``, unreadable
    job JSON, an import failure under a wrong ``VQM_PKG_ROOT``) would otherwise let
    Blender exit 0 with no report written and read as a false PASS. Two guards
    prevent that: Blender is told to exit non-zero on an unhandled exception, and a
    zero exit with no report file is treated as a hard failure.
    """
    report_dir = out_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{name}.json"
    if report_path.exists():
        report_path.unlink()

    studios = inputs.weapon_studios or (inputs.weapon_pv,)
    job = {
        "reference": str(inputs.reference),
        "weapon_pv": str(inputs.weapon_pv),
        "weapon_studios": [str(p) for p in studios],
        "original_hands": str(inputs.original_hands),
        "sequence": {"name": name, "path": str(inputs.sequences[name])},
        "out_dir": str(out_dir),
        "report": str(report_path),
        "dry_run": dry_run,
        "export": export,
        "export_mesh": export_mesh,
        "weapon_stem": weapon_stem,
        "weapon_stems": [p.stem for p in studios],
        "hand_variants": {n: str(p) for n, p in inputs.hand_variants.items()},
        "config": config.to_job_dict(),
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(job, handle)
        job_path = handle.name

    env = dict(os.environ)
    env["VQM_PKG_ROOT"] = str(Path(__file__).resolve().parents[2])  # the src/ dir
    try:
        proc = subprocess.run(
            [blender, "--background", "--factory-startup", "--python-exit-code", "1",
             "--python", str(WORKER), "--", "--job", job_path],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return SequenceResult(
            name, EXIT_ENV,
            {"status": "FAIL", "error": f"worker timed out after {timeout}s", "kind": "timeout"},
            _as_text(exc.stdout), _as_text(exc.stderr),
        )
    finally:
        os.unlink(job_path)

    if not report_path.exists():
        # Worker died before writing a report; Blender may still report 0.
        code = proc.returncode or EXIT_ENV
        return SequenceResult(
            name, code,
            {"status": "FAIL", "error": f"worker wrote no report (exit {proc.returncode})",
             "kind": "worker-crash"},
            proc.stdout, proc.stderr,
        )
    report = json.loads(report_path.read_text())
    return SequenceResult(name, proc.returncode, report, proc.stdout, proc.stderr)


def finalize_export(
    inputs: Inputs,
    out_dir: Path,
    results: list[SequenceResult],
    config: RetargetConfig,
    *,
    qc_path: Path | None = None,
) -> tuple[VerifyResult | None, Path | None]:
    """Phase 6 gate + QC generation over the emitted model.

    Parses the written SMDs, proves the §2 constraints (:func:`verify_export`),
    and rewrites the input QC to compile the merged mesh + retargeted anims. The
    node-bookkeeping (reference bones, anchors, gun bones) comes from any exported
    worker report, since every sequence shares the same unified skeleton.
    """
    # One weapon SMD per always-on $bodygroup "weapon" studio (a multi-part
    # weapon keeps each part a separate submodel); the first part is the primary.
    weapon_stems = [p.stem for p in inputs.weapon_studios] or [inputs.weapon_pv.stem]
    primary_stem = weapon_stems[0]
    # Expected mesh SMDs: hands+weapon merged into the first part when there are
    # no variants; otherwise weapon-only parts plus one hands_<name> per variant.
    mesh_smds = {stem: out_dir / f"{stem}.smd" for stem in weapon_stems}
    mesh_sources: dict[str, Path] = {}
    if inputs.hand_variants:
        for variant, source in inputs.hand_variants.items():
            exported_name = f"hands_{variant}"
            mesh_smds[exported_name] = out_dir / f"{exported_name}.smd"
            mesh_sources[exported_name] = source
    else:
        mesh_sources[primary_stem] = inputs.reference

    def _has_export(result: SequenceResult) -> bool:
        block = result.report.get("export")
        return result.ok and isinstance(block, dict) and bool(block.get("gun_bones"))

    exported = next((r for r in results if _has_export(r)), None)
    if exported is None:
        return None, None  # nothing attempted export (e.g. dry run or all failed)

    # Every ok sequence must have produced its SMD; a missing file is a hard
    # verify failure, not a silent exclusion (the QC would still reference it).
    anim_smds = {r.name: out_dir / "anims" / f"{r.name}.smd" for r in results if r.ok}
    missing = sorted(n for n, p in anim_smds.items() if not p.exists())
    missing_meshes = sorted(n for n, p in mesh_smds.items() if not p.exists())
    if missing_meshes or missing:
        failed = VerifyResult()
        if missing_meshes:
            failed.fail("outputs_present", f"mesh SMDs missing: {missing_meshes}")
        if missing:
            failed.fail("outputs_present",
                        f"anim SMDs missing for ok sequences: {missing}")
        return failed, None

    report = exported.report
    export_block = report["export"]
    assert isinstance(export_block, dict)
    reference_bones = _str_set(report.get("reference_bones"))
    anchor_bones = _str_set(report.get("anchor_bones"))
    gun_bones = _str_set(export_block.get("gun_bones"))

    # §7.8: BST writes each frame's Euler from the pose matrix independently, so
    # unwrap the emitted tracks in place before the continuity check can pass.
    for path in anim_smds.values():
        unwrapped, _changed = unwrap_smd(parse_smd_file(path))
        write_smd_file(unwrapped, path)
    # Normalise BST's flat mesh output to the classic indented SMD layout the
    # GoldSource studiomdl toolchain compiles (the writer emits it).
    for path in mesh_smds.values():
        write_smd_file(parse_smd_file(path), path)

    # Delivery contract: normalise material names in the exported meshes (ASCII,
    # no spaces, .bmp extension — studiomdl may refuse otherwise) and stage each
    # referenced 8-bit BMP next to the QC.
    search_dirs: list[Path] = []
    for candidate in (inputs.weapon_pv.parent, inputs.reference.parent,
                      *(p.parent for p in inputs.hand_variants.values())):
        if candidate not in search_dirs:
            search_dirs.append(candidate)
    textures = finalize_textures(out_dir, mesh_smds, search_dirs)

    verify = verify_export(
        mesh_smds, anim_smds, inputs.reference,
        hand_bones=reference_bones, anchor_bones=anchor_bones,
        mesh_sources=mesh_sources,
        source_anims=dict(inputs.sequences),
        gun_bones=gun_bones,
        weapon_offset=config.weapon_offset,
        epsilon=config.epsilon,
        euler_jump_threshold_degrees=config.euler_jump_threshold_degrees,
        geom_tolerance=config.geom_tolerance,
    )
    for error in textures.errors:
        verify.fail("textures_valid", error)
    if not textures.errors:
        verify.passed("textures_valid")
    verify.warnings.extend(textures.warnings)

    qc_out: Path | None = None
    qc_src = qc_path or _find_qc(inputs.weapon_pv.parent)
    if qc_src is not None and qc_src.exists():
        qc_text_src = qc_src.read_text()
        model_stem = modelname(qc_text_src, primary_stem).removesuffix(".mdl") or primary_stem
        qc_text = build_qc(
            qc_text_src,
            mesh_stem=primary_stem,
            anims_subdir="anims",
            surviving_bones=reference_bones | gun_bones,
            model_name=f"{model_stem}.mdl",
            hand_bodies=sorted(f"hands_{v}" for v in inputs.hand_variants) or None,
            weapon_bodies=weapon_stems,
        )
        # Single-part: keep the studio-stem filename (unchanged output). Multi-part:
        # name after the model ($modelname) so the folder isn't named after one
        # part (v_bloodhunter.qc, not v_bloodhunter_left.qc).
        qc_stem = primary_stem if len(weapon_stems) == 1 else model_stem
        qc_out = out_dir / f"{qc_stem}.qc"
        qc_out.write_text(qc_text)
    return verify, qc_out


def _str_set(value: object) -> set[str]:
    """Coerce a report field (JSON list of strings) into a set of names."""
    if isinstance(value, (list, tuple, set)):
        return {str(item) for item in value}
    return set()


def _find_qc(weapon_dir: Path) -> Path | None:
    matches = sorted(glob(str(weapon_dir / "*.qc")))
    return Path(matches[0]) if matches else None


__all__ = [
    "DriverError",
    "Inputs",
    "SequenceResult",
    "find_blender",
    "resolve_inputs",
    "assert_identical_node_tables",
    "assert_variant_skeletons",
    "run_sequence",
    "finalize_export",
]
