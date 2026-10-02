"""Decompile ``.mdl`` files in process (no external decompiler)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from valve_qc_merger.mdl.decompile import decompile_mdl
from valve_qc_merger.mdl.reader import MdlError
from valve_qc_merger.services.base import (
    EXIT_DISCOVERY,
    EXIT_FAIL,
    EXIT_OK,
    Reporter,
    ServiceResult,
)

_GROUP_RE = re.compile(r"^(?P<stem>.+?)\d\d$")


@dataclass
class DecompileOptions:
    source: Path  # one .mdl, or a folder searched recursively
    out: Path  # each model lands in out/<model stem>/


def is_companion(path: Path) -> bool:
    """``<name>T.mdl`` (external textures) and ``<name>01.mdl`` (sequence
    group) files belong to a main model next to them."""
    stem = path.stem
    if stem[-1:] in ("T", "t") and path.with_name(stem[:-1] + path.suffix).exists():
        return True
    match = _GROUP_RE.match(stem)
    if match and path.with_name(match.group("stem") + path.suffix).exists():
        return True
    with open(path, "rb") as handle:
        return handle.read(4) == b"IDSQ"


def find_models(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    return sorted(p for p in source.rglob("*") if p.suffix.lower() == ".mdl"
                  and p.is_file() and not is_companion(p))


def run_decompile(opts: DecompileOptions, reporter: Reporter | None = None) -> ServiceResult:
    reporter = reporter or Reporter()
    result = ServiceResult()
    if not opts.source.exists():
        reporter.log(f"error: not found: {opts.source}")
        result.exit_code = EXIT_DISCOVERY
        return result
    models = find_models(opts.source)
    if not models:
        reporter.log(f"error: no .mdl in {opts.source}")
        result.exit_code = EXIT_DISCOVERY
        return result
    for done, mdl in enumerate(models):
        reporter.check()
        reporter.progress(done, len(models), mdl.name)
        try:
            outcome = decompile_mdl(mdl, opts.out)
        except (MdlError, OSError, ValueError, IndexError) as exc:
            reporter.log(f"  {mdl.name:<28} FAIL  {exc}")
            result.failures.append(f"{mdl.name}: {exc}")
            continue
        result.outputs.append(outcome.qc)
        result.warnings.extend(f"{mdl.name}: {w}" for w in outcome.warnings)
        note = f"  ({len(outcome.warnings)} warnings)" if outcome.warnings else ""
        reporter.log(f"  {mdl.name:<28} OK    -> {outcome.directory.name}{note}")
        for warning in outcome.warnings:
            reporter.log(f"    warn: {warning}")
    reporter.progress(len(models), len(models), "done")
    result.exit_code = EXIT_FAIL if result.failures else EXIT_OK
    return result


__all__ = ["DecompileOptions", "find_models", "is_companion", "run_decompile"]
