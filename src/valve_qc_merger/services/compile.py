"""Compile a QC with studiomdl as a service (streamed log, cancellable)."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from valve_qc_merger.services.base import (
    EXIT_DISCOVERY,
    EXIT_FAIL,
    EXIT_OK,
    Cancelled,
    Reporter,
    ServiceResult,
)

_MODELNAME_RE = re.compile(r'^\s*\$modelname\s+"?([^"\s]+)"?', re.MULTILINE | re.IGNORECASE)


@dataclass
class CompileOptions:
    qc: Path
    studiomdl: Path
    normalize: bool = True  # LF line endings + forward slashes in the QC
    preflight: bool = True  # check the QC first (services/qc_check.py)


def normalize_sources(qc: Path) -> None:
    """Make the QC directory portable across studiomdl builds: LF endings in
    every .qc/.smd and ``\\`` -> ``/`` in the QC's paths (the macOS-native
    compiler reads a backslash as part of the file name; Windows accepts
    both)."""
    for path in [*qc.parent.rglob("*.smd"), *qc.parent.rglob("*.qc")]:
        raw = path.read_bytes()
        fixed = raw.replace(b"\r\n", b"\n")
        if path == qc:
            fixed = fixed.replace(b"\\", b"/")
        if fixed != raw:
            path.write_bytes(fixed)


def compiled_model_path(qc: Path) -> Path:
    """Where studiomdl writes the .mdl: ``$modelname`` relative to the QC."""
    match = _MODELNAME_RE.search(qc.read_text(encoding="latin-1"))
    name = match.group(1) if match else qc.with_suffix(".mdl").name
    return qc.parent / name.replace("\\", "/")


def run_compile(opts: CompileOptions, reporter: Reporter | None = None) -> ServiceResult:
    """Run studiomdl on ``opts.qc`` inside the QC's directory."""
    reporter = reporter or Reporter()
    result = ServiceResult()
    if not opts.qc.is_file():
        reporter.log(f"error: QC not found: {opts.qc}")
        result.exit_code = EXIT_DISCOVERY
        return result
    if not opts.studiomdl.is_file():
        reporter.log(f"error: studiomdl not found: {opts.studiomdl}")
        result.exit_code = EXIT_DISCOVERY
        return result
    if opts.normalize:
        normalize_sources(opts.qc)
    if opts.preflight:
        from valve_qc_merger.services.qc_check import check_qc
        try:
            from valve_qc_merger import limits
            problems = check_qc(opts.qc, limits.studiomdl_submodels(opts.studiomdl))
        except (OSError, ValueError) as exc:  # the check never blocks on itself
            reporter.log(f"  warn: QC check skipped: {exc}")
            problems = []
        errors = [p for p in problems if p.level == "error"]
        for problem in problems:
            reporter.log(f"  {'error' if problem.level == 'error' else 'warn'}: "
                         f"{opts.qc.name}: {problem.message}")
        if errors:
            result.failures.append(f"{opts.qc.name}: {errors[0].message}"
                                   + (f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""))
            result.exit_code = EXIT_FAIL
            return result
    target = compiled_model_path(opts.qc)
    # Remove a stale .mdl so success means "studiomdl wrote it just now":
    # some studiomdl builds exit 0 after printing an Error.
    target.unlink(missing_ok=True)
    reporter.log(f"  studiomdl {opts.qc.name}")
    process = subprocess.Popen(
        [str(opts.studiomdl.resolve()), opts.qc.name], cwd=opts.qc.parent,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, text=True, errors="replace",
    )
    assert process.stdout is not None
    try:
        for line in process.stdout:
            reporter.log(f"    {line.rstrip()}")
            if reporter.cancelled():
                raise Cancelled("cancelled")
        code = process.wait()
    except Cancelled:
        process.kill()
        process.wait()
        raise
    if code != 0 or not target.exists():
        reporter.log(f"error: studiomdl failed (exit {code}) for {opts.qc.name}")
        result.failures.append(f"{opts.qc.name}: studiomdl exit {code}")
        result.exit_code = EXIT_FAIL
        return result
    result.outputs.append(target)
    reporter.log(f"  -> {target.name} ({target.stat().st_size} bytes)")
    result.exit_code = EXIT_OK
    return result


__all__ = ["CompileOptions", "compiled_model_path", "normalize_sources", "run_compile"]
