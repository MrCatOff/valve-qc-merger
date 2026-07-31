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
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path

from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget.config import RetargetConfig

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


def _one(matches: list[str], what: str, where: Path) -> Path:
    if not matches:
        raise DriverError(f"no {what} found in {where}")
    if len(matches) > 1:
        raise DriverError(f"multiple {what} in {where}: {[Path(m).name for m in matches]}")
    return Path(matches[0])


def resolve_inputs(
    reference: Path,
    weapon_dir: Path,
    anims_glob: str,
    *,
    weapon_pv: Path | None = None,
    original_hands: Path | None = None,
    only: set[str] | None = None,
) -> Inputs:
    """Discover the weapon mesh, original hands and animation set."""
    weapon_dir = weapon_dir.resolve()
    pv = weapon_pv or _one(
        sorted(glob(str(weapon_dir / "*-PV.smd"))), "*-PV.smd weapon mesh", weapon_dir
    )
    hands = original_hands or _one(
        sorted(glob(str(weapon_dir / "f_*_Male_hand_Low.smd")))
        or sorted(glob(str(weapon_dir / "f_*_hand_Low.smd"))),
        "original hand mesh (f_*_hand_Low.smd)",
        weapon_dir,
    )
    anim_paths = sorted(glob(str(weapon_dir / anims_glob)))
    if not anim_paths:
        raise DriverError(f"no animations matched {anims_glob!r} under {weapon_dir}")
    sequences = {Path(p).stem: Path(p) for p in anim_paths}
    if only is not None:
        missing = only - sequences.keys()
        if missing:
            raise DriverError(f"requested sequences not found: {sorted(missing)}")
        sequences = {name: sequences[name] for name in sorted(only)}
    return Inputs(reference.resolve(), pv, hands, sequences)


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

    job = {
        "reference": str(inputs.reference),
        "weapon_pv": str(inputs.weapon_pv),
        "original_hands": str(inputs.original_hands),
        "sequence": {"name": name, "path": str(inputs.sequences[name])},
        "out_dir": str(out_dir),
        "report": str(report_path),
        "dry_run": dry_run,
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


__all__ = [
    "DriverError",
    "Inputs",
    "SequenceResult",
    "find_blender",
    "resolve_inputs",
    "assert_identical_node_tables",
    "run_sequence",
]
