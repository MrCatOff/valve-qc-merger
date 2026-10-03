"""zhands-grenade as a service: give zombie hands a grenade.

A zombie's knife model has its hands; the grenade donor (default: the
bundled ``storage/zhands/grenade_donor/v_banshee_grenade`` — the frog bomb
and four sequences idle/pullpin/throw/deploy) has the grenade and the hand
motion. The donor's hands are swapped for the zombie's with the retarget
engine: a hands asset is built from the knife model (its own textures kept,
the knife blade left out), the donor is retargeted onto it, and the result is
written as ``v_<zombie>_grenade`` — ready for merge-zhands.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import types
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.resources import resource_path
from valve_qc_merger.services.base import (
    EXIT_DISCOVERY,
    EXIT_FAIL,
    EXIT_OK,
    Reporter,
    ServiceResult,
)

DEFAULT_DONOR = Path("storage/zhands/grenade_donor/v_banshee_grenade")


@dataclass
class ZhandsGrenadeOptions:
    knife_dir: Path
    out: Path
    donor: Path | None = None  # None: the bundled banshee grenade
    modelname: str | None = None  # None: from the output folder name
    snug_max_deg: float | None = None
    curl: list[str] = field(default_factory=list)
    grip_offset: list[str] = field(default_factory=list)
    weapon_offset: list[float] = field(default_factory=list)


def grenade_name(knife: str) -> str:
    """``v_heavy_knife`` -> ``v_heavy_grenade`` (variants keep their tail:
    ``v_ghost_knife_alternate`` -> ``v_ghost_grenade_alternate``)."""
    named = re.sub(r"_knife(?=_|$)", "_grenade", knife, count=1)
    return named if named != knife else f"{knife}_grenade"


def _used_materials(directory: Path) -> set[str]:
    """Materials (lower case) the reference SMDs of ``directory`` use: in a
    ``triangles`` block every fourth line names one."""
    used: set[str] = set()
    for smd in directory.glob("*.smd"):
        text = smd.read_text(encoding="latin-1", errors="replace")
        match = re.search(r"^triangles\s*$(.*?)^end\s*$", text, re.MULTILINE | re.DOTALL)
        if match:
            lines = [line.strip() for line in match.group(1).strip().splitlines()]
            used.update(line.lower() for line in lines[0::4] if line)
    return used


def run_zhands_grenade(opts: ZhandsGrenadeOptions,
                       reporter: Reporter | None = None) -> ServiceResult:
    from valve_qc_merger.handswap import convert as convertmod
    from valve_qc_merger.handswap.foreign import asset_from_weapon, write_asset

    reporter = reporter or Reporter()
    result = ServiceResult()
    donor = resource_path(opts.donor or DEFAULT_DONOR)
    if not opts.knife_dir.is_dir() or not donor.is_dir():
        missing = opts.knife_dir if not opts.knife_dir.is_dir() else donor
        reporter.log(f"error: not found: {missing}")
        result.exit_code = EXIT_DISCOVERY
        return result

    def log(message: str) -> None:
        reporter.log(f"[handswap] {message}")

    out = opts.out
    with tempfile.TemporaryDirectory() as tmp:
        try:
            raw = asset_from_weapon(str(opts.knife_dir), keep_materials=True, log=log)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            reporter.log(f"error: no usable hands in {opts.knife_dir.name}: {exc}")
            result.exit_code = EXIT_DISCOVERY
            return result
        asset_path = write_asset(raw, str(Path(tmp) / "hands.json.gz"))
        reporter.log(f"  hands from {opts.knife_dir.name}: {len(raw['triangles'])} triangles, "
                     f"textures {', '.join(raw.get('textures', {})) or '-'}")
        if out.exists():
            shutil.rmtree(out)
        args = types.SimpleNamespace(
            weapon_dir=str(donor), out=str(out), qc=None, asset=asset_path,
            hands_texture=None, modelname=opts.modelname or f"{out.name}.mdl",
            studiomdl=convertmod.DEFAULT_STUDIOMDL, compile=False, verify=True, snug=True,
            snug_max_deg=opts.snug_max_deg, curl=list(opts.curl),
            grip_offset=list(opts.grip_offset), weapon_offset=list(opts.weapon_offset) or None)
        try:
            info = convertmod.convert(args, log=log)
        except (RuntimeError, FileNotFoundError, ValueError) as exc:
            reporter.log(f"error: {exc}")
            result.failures.append(str(exc))
            result.exit_code = EXIT_FAIL
            return result

    # the donor's own hand texture and the default hands slot are unused now
    used = _used_materials(out)
    for bmp in out.glob("*.[bB][mM][pP]"):
        if bmp.name.lower() not in used:
            bmp.unlink()
    # one QC named after the model (the donor's QC name means nothing here)
    qcs = sorted(out.glob("*.qc"))
    if qcs and qcs[0].name != f"{out.name}.qc":
        qcs[0].rename(out / f"{out.name}.qc")
    report = info.get("verify") if isinstance(info, dict) else None
    if report is not None and not report.get("ok", True):
        result.failures.append("verification failed")
        result.exit_code = EXIT_FAIL
        return result
    result.outputs.extend(sorted(out.glob("*.qc")))
    reporter.log(f"  -> {out}")
    result.exit_code = EXIT_OK
    return result


__all__ = ["DEFAULT_DONOR", "ZhandsGrenadeOptions", "grenade_name", "run_zhands_grenade"]
