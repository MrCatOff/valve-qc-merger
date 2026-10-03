"""Round-trip benchmark for the hand retarget: a measured error, not a look.

A *native* weapon already wears our exact hands (CSO 2009 hand set — 154 of
the CSO corpus). Two retargets are compared with it, frame by frame:

* **identity** — native -> ours directly: what the engine itself loses when
  nothing needs to change (rig differences, snug side effects);
* **round trip** — native -> foreign hands (e.g. the old Valve hands of the
  classic v_deagle, :mod:`.foreign`) -> ours: the real job, size change and
  back.

Error = distance from our hand's SKIN to the native hand's skin: for sampled
skin vertices of ours, the distance to the nearest point of the native
surface, at sampled frames of every sequence. ``mean`` is the GRIP — palm
and fingers; the arm is reported apart (``arm``): its direction follows the
weapon by design (off-screen shoulders), the same whatever the hand size,
and it would otherwise drown the grip. It needs no correspondence
between the rigs — two rigs of the same mesh weight its vertices differently,
so bone-based measures (segment centroids) have a floor even for a perfect
result; surface distance is zero exactly when the hands look the same. Our
hands are the CSO 2009 male mesh 1:1, so the native male mesh is compared;
``tips`` restricts our side to the distal finger segments.
"""

from __future__ import annotations

import json
import os
import shutil
import types
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from . import asset as assetmod
from .surface import points_to_mesh


@dataclass
class SequenceError:
    name: str
    frames: int
    mean: float
    p95: float
    max: float
    tips_mean: float  # distal segments only (where a grip shows)


@dataclass
class BenchResult:
    weapon: str
    mode: str  # "identity" | "roundtrip"
    mean: float = float("nan")
    p95: float = float("nan")
    max: float = float("nan")
    tips_mean: float = float("nan")
    arm_mean: float = float("nan")
    worst_sequence: str = ""
    sequences: list[SequenceError] = field(default_factory=list)
    error: str = ""


def native_hand_meshes(weapon_dir: Path) -> list[str]:
    """Stems of the CSO 2009 hand meshes a native weapon carries."""
    return sorted(p.stem for p in Path(weapon_dir).glob("*.smd")
                  if "hand" in p.stem.lower() and "2009" in p.stem)


def is_native(weapon_dir: Path) -> bool:
    return bool(native_hand_meshes(weapon_dir))


FRAME_SAMPLES = 3  # frames measured per sequence (first, middle, last)
POINT_SAMPLES = 200  # skin vertices sampled per hand mesh


def _posed_hands(scene, stems: set[str], seq: int, frame: float):  # noqa: ANN001
    """(triangles (T,3,3), vertices (V,3), bone index per vertex) of a pose."""
    rot, trans = scene.world(*scene.local_pose(seq, frame))
    tris, verts, bones = [], [], []
    for batch in scene.batches:
        if batch.stem in stems:
            pos, _n = scene.skin(batch, rot, trans)
            tris.append(pos.reshape(-1, 3, 3))
            verts.append(pos)
            bones.append(batch.bones)
    return np.concatenate(tris), np.concatenate(verts), np.concatenate(bones)


def compare(native_dir: Path, result_dir: Path, mode: str) -> BenchResult:
    """Hand-surface error of ``result_dir`` against ``native_dir``."""
    from valve_qc_merger.studio.scene import build_scene
    meshes = native_hand_meshes(native_dir)
    male = [m for m in meshes if "female" not in m.lower()] or meshes
    native = build_scene(Path(native_dir))
    ours = build_scene(Path(result_dir))
    tip_bones = {i for i, n in enumerate(ours.bone_names) if n.endswith(("02.L", "02.R"))}
    arm_bones = {i for i, n in enumerate(ours.bone_names)
                 if n.startswith(("UpperArm", "Arm0", "Arm1"))}
    rng = np.random.default_rng(0)
    result = BenchResult(weapon=Path(native_dir).name, mode=mode)
    every: list[np.ndarray] = []
    tip_all: list[np.ndarray] = []
    arm_all: list[np.ndarray] = []
    ours_index = {s.name: i for i, s in enumerate(ours.sequences)}
    for s, seq in enumerate(native.sequences):
        o = ours_index.get(seq.name)
        if o is None or ours.sequences[o].frames != seq.frames:
            continue
        frames = np.unique(np.linspace(0, seq.frames - 1, min(FRAME_SAMPLES, seq.frames))
                           .round().astype(int))
        errs, tips, arms = [], [], []
        for f in frames:
            n_tris, _nv, _nb = _posed_hands(native, {male[0]}, s, float(f))
            _ot, o_verts, o_bones = _posed_hands(ours, {"hands"}, o, float(f))
            is_arm = np.isin(o_bones, list(arm_bones))
            grip, arm = np.flatnonzero(~is_arm), np.flatnonzero(is_arm)
            pick = rng.choice(grip, min(POINT_SAMPLES, len(grip)), replace=False)
            to_native = points_to_mesh(o_verts[pick], n_tris)
            errs.append(to_native)
            tips.append(to_native[np.isin(o_bones[pick], list(tip_bones))])
            if len(arm):
                pick_arm = rng.choice(arm, min(POINT_SAMPLES // 4, len(arm)), replace=False)
                arms.append(points_to_mesh(o_verts[pick_arm], n_tris))
        err = np.concatenate(errs)
        tip = np.concatenate(tips)
        every.append(err)
        tip_all.append(tip)
        arm_all.extend(arms)
        result.sequences.append(SequenceError(
            seq.name, seq.frames, float(err.mean()), float(np.percentile(err, 95)),
            float(err.max()), float(tip.mean()) if len(tip) else float("nan")))
    if every:
        allv = np.concatenate(every)
        result.mean, result.p95, result.max = (float(allv.mean()),
                                               float(np.percentile(allv, 95)),
                                               float(allv.max()))
        tipv = np.concatenate(tip_all)
        result.tips_mean = float(tipv.mean()) if len(tipv) else float("nan")
        if arm_all:
            result.arm_mean = float(np.concatenate(arm_all).mean())
        result.worst_sequence = max(result.sequences, key=lambda s: s.mean).name
    return result


def _retarget(weapon_dir: Path, out: Path, asset: str | None = None,
              texture: str | None = None) -> None:
    from . import convert as convertmod
    args = types.SimpleNamespace(
        weapon_dir=str(weapon_dir), out=str(out), qc=None,
        asset=asset or assetmod.DEFAULT_ASSET, hands_texture=texture, modelname=None,
        studiomdl=convertmod.DEFAULT_STUDIOMDL, compile=False, verify=False, snug=True,
        snug_max_deg=None, curl=[], grip_offset=[], weapon_offset=None)
    convertmod.convert(args, log=lambda *a: None)


def run_weapon(weapon_dir: Path, foreign_asset: str, work: Path,
               foreign_texture: str | None = None) -> list[BenchResult]:
    """identity + round trip for one native weapon (results in ``work``)."""
    name = Path(weapon_dir).name
    out: list[BenchResult] = []
    for mode in ("identity", "roundtrip"):
        try:
            back = work / mode / name
            shutil.rmtree(work / mode / name, ignore_errors=True)
            if mode == "identity":
                _retarget(weapon_dir, back)
            else:
                foreign = work / "foreign" / name
                shutil.rmtree(foreign, ignore_errors=True)
                _retarget(weapon_dir, foreign, foreign_asset, foreign_texture)
                _retarget(foreign, back)
            out.append(compare(weapon_dir, back, mode))
        except Exception as exc:  # noqa: BLE001 - one bad weapon must not stop a corpus run
            out.append(BenchResult(weapon=name, mode=mode, error=f"{type(exc).__name__}: {exc}"))
    return out


def write_report(results: list[BenchResult], path: Path) -> None:
    """``<path>.json`` (everything) and ``<path>.md`` (the table)."""
    path.with_suffix(".json").write_text(json.dumps([asdict(r) for r in results], indent=1))
    by_weapon: dict[str, dict[str, BenchResult]] = {}
    for r in results:
        by_weapon.setdefault(r.weapon, {})[r.mode] = r
    lines = ["| weapon | identity grip | round trip grip | round trip p95 | tips | arm | "
             "worst sequence |", "|---|---|---|---|---|---|---|"]
    rows = sorted(by_weapon.items(),
                  key=lambda kv: -(kv[1].get("roundtrip").mean
                                   if kv[1].get("roundtrip") and not kv[1]["roundtrip"].error
                                   else -1))
    for weapon, modes in rows:
        ident, trip = modes.get("identity"), modes.get("roundtrip")

        def cell(r: BenchResult | None, attr: str) -> str:
            if r is None:
                return "—"
            return "error" if r.error else f"{getattr(r, attr):.3f}"
        lines.append(f"| {weapon} | {cell(ident, 'mean')} | {cell(trip, 'mean')} | "
                     f"{cell(trip, 'p95')} | {cell(trip, 'tips_mean')} | "
                     f"{cell(trip, 'arm_mean')} | "
                     f"{trip.worst_sequence if trip and not trip.error else ''} |")
    ok = [m["roundtrip"] for m in by_weapon.values()
          if "roundtrip" in m and not m["roundtrip"].error]
    if ok:
        idents = [m["identity"] for m in by_weapon.values()
                  if "identity" in m and not m["identity"].error]
        lines.insert(0, f"Round trip over {len(ok)} weapons: grip "
                        f"{np.mean([r.mean for r in ok]):.3f} u, tips "
                        f"{np.nanmean([r.tips_mean for r in ok]):.3f} u, arm "
                        f"{np.nanmean([r.arm_mean for r in ok]):.3f} u; identity grip "
                        f"{np.mean([r.mean for r in idents]):.3f} u\n")
    path.with_suffix(".md").write_text("\n".join(lines) + "\n")


__all__ = ["BenchResult", "compare", "is_native", "native_hand_meshes", "run_weapon",
           "write_report"]
