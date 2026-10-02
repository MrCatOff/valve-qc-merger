"""retarget as a service: swap a viewmodel's hands for the CSO hands."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from valve_qc_merger.resources import resource_path
from valve_qc_merger.services.base import (
    EXIT_DISCOVERY,
    EXIT_FAIL,
    EXIT_OK,
    Reporter,
    ServiceResult,
)


@dataclass
class RetargetOptions:
    weapon_dir: Path
    category: str = "uncategorized"
    out: Path | None = None  # None: storage/retarget/{category}/{model}
    qc: Path | None = None
    asset: Path | None = None
    hands_texture: Path | None = None
    modelname: str | None = None
    studiomdl: Path | None = None
    compile: bool = False
    verify: bool = True
    snug: bool = True
    snug_max_deg: float = 18.0
    curl: list[str] = field(default_factory=list)
    grip_offset: list[str] = field(default_factory=list)
    weapon_offset: list[float] = field(default_factory=list)  # [dx, dy, dz], model space


def resolve_out_dir(opts: RetargetOptions) -> Path:
    """An explicit ``out`` wins; otherwise the model lands under
    ``storage/retarget/{category}/{model}`` (model = weapon-dir name), the
    layout the merge commands consume."""
    if opts.out is not None:
        return opts.out
    category = opts.category.strip()
    if not category or "/" in category or "\\" in category or \
            category in {".", ".."}:
        raise ValueError(f"invalid --category {opts.category!r} "
                         "(must be a plain name)")
    model = opts.weapon_dir.name or opts.weapon_dir.resolve().name
    return resource_path(Path("storage") / "retarget" / category / model)


def run_retarget(opts: RetargetOptions, reporter: Reporter | None = None) -> ServiceResult:
    """Convert one decompiled weapon onto the CSO hands."""
    from valve_qc_merger.handswap import convert as convertmod

    reporter = reporter or Reporter()
    result = ServiceResult()
    weapon_dir = opts.weapon_dir
    if not weapon_dir.is_dir():
        reporter.log(f"error: weapon dir not found: {weapon_dir}")
        result.exit_code = EXIT_DISCOVERY
        return result
    try:
        out_dir = resolve_out_dir(opts)
    except ValueError as exc:
        reporter.log(f"error: {exc}")
        result.exit_code = EXIT_DISCOVERY
        return result

    # Build the namespace handswap.convert.convert() expects; leaving an
    # optional at None lets the engine fall back to its own default.
    conv_args = SimpleNamespace(
        weapon_dir=str(weapon_dir),
        out=str(out_dir),
        qc=(str(opts.qc) if opts.qc else None),
        asset=(str(resource_path(opts.asset)) if opts.asset
               else convertmod.assetmod.DEFAULT_ASSET),
        hands_texture=(str(resource_path(opts.hands_texture))
                       if opts.hands_texture else None),
        modelname=opts.modelname,
        studiomdl=(str(resource_path(opts.studiomdl)) if opts.studiomdl
                   else convertmod.DEFAULT_STUDIOMDL),
        compile=opts.compile,
        verify=opts.verify,
        snug=opts.snug,
        snug_max_deg=opts.snug_max_deg,
        curl=opts.curl,
        grip_offset=opts.grip_offset,
        weapon_offset=list(opts.weapon_offset) or None,
    )

    def engine_log(message: str) -> None:
        reporter.log(f"[handswap] {message}")

    reporter.log(f"  output: {out_dir}")
    try:
        info = convertmod.convert(conv_args, log=engine_log)
    except FileNotFoundError as exc:
        reporter.log(f"error: {exc}")
        result.exit_code = EXIT_DISCOVERY
        return result
    except RuntimeError as exc:
        # no hands found => discovery; verify/compile failure => fail
        message = str(exc)
        reporter.log(f"error: {message}")
        result.failures.append(message)
        lowered = message.lower()
        result.exit_code = (EXIT_FAIL if "verification" in lowered
                            or "studiomdl" in lowered else EXIT_DISCOVERY)
        return result

    result.data["info"] = info
    qcs = sorted(out_dir.glob("*.qc"))
    result.outputs.extend(qcs)
    report = info.get("verify")
    if report is not None and not report.get("ok", True):
        result.failures.append("verification failed")
        result.exit_code = EXIT_FAIL
    else:
        result.exit_code = EXIT_OK
    return result


__all__ = ["RetargetOptions", "resolve_out_dir", "run_retarget"]
