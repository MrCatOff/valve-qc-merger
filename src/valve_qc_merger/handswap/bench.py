"""Round-trip benchmark for the hand retarget: a measured error, not a look.

A *native* weapon already wears our exact hands (CSO 2009 hand set — 154 of
the CSO corpus). Two retargets are compared with it, frame by frame:

* **identity** — native -> ours directly: what the engine itself loses when
  nothing needs to change (rig differences, snug side effects);
* **round trip** — native -> foreign hands (e.g. the old Valve hands of the
  classic v_deagle, :mod:`.foreign`) -> ours: the real job, size change and
  back.

Error = distance between the same hand segment in the native model and in
the result, every frame of every sequence. A segment is measured where it is
VISIBLE: the centroid of the hand-mesh vertices skinned to that bone (bone
joints of two different rigs need not coincide, the skin does: our hands are
the CSO 2009 mesh 1:1). Bones pair the way the engine pairs them (the plan of
the native hands against our asset).
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
from . import build as buildmod
from .retarget import build_plan


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
    worst_sequence: str = ""
    sequences: list[SequenceError] = field(default_factory=list)
    error: str = ""


def native_hand_meshes(weapon_dir: Path) -> list[str]:
    """Stems of the CSO 2009 hand meshes a native weapon carries."""
    return sorted(p.stem for p in Path(weapon_dir).glob("*.smd")
                  if "hand" in p.stem.lower() and "2009" in p.stem)


def is_native(weapon_dir: Path) -> bool:
    return bool(native_hand_meshes(weapon_dir))


def pairing(weapon_dir: Path) -> dict[str, str]:
    """native bone -> our asset bone, as the engine pairs them."""
    model = buildmod.load_weapon(str(weapon_dir), None, log=lambda *a: None)
    first = model.qc.sequences[0]["smd"]
    setup = buildmod.anim_world_frames(model.anims[first], model.skel)[0]
    plan = build_plan(assetmod.load(), model.skel, model.hands, setup, log=lambda *a: None)
    out: dict[str, str] = {}
    for sp in plan.sides:
        out[sp.hand.wrist] = sp.rig.wrist
        for old_chain, cso_chain in sp.pairs:
            out.update(zip(old_chain, cso_chain, strict=False))
    return out


def segment_centroids(weapon_dir: Path, stems: set[str], bones: list[str]
                      ) -> dict[str, np.ndarray]:
    """sequence -> (frames, len(bones), 3) world centroid of each bone's
    skinned vertices in the given meshes (NaN where a bone has none)."""
    from valve_qc_merger.studio.scene import build_scene
    scene = build_scene(Path(weapon_dir))
    index = {n: i for i, n in enumerate(scene.bone_names)}
    wanted = [index.get(b, -1) for b in bones]
    batches = [b for b in scene.batches if b.stem in stems]
    out: dict[str, np.ndarray] = {}
    for s, seq in enumerate(scene.sequences):
        frames = np.full((seq.frames, len(bones), 3), np.nan)
        for f in range(seq.frames):
            rot, trans = scene.world(*scene.local_pose(s, float(f)))
            sums = np.zeros((len(scene.bone_names), 3))
            counts = np.zeros(len(scene.bone_names))
            for batch in batches:
                pos, _n = scene.skin(batch, rot, trans)
                np.add.at(sums, batch.bones, pos)
                np.add.at(counts, batch.bones, 1)
            for j, i in enumerate(wanted):
                if i >= 0 and counts[i]:
                    frames[f, j] = sums[i] / counts[i]
        out[seq.name] = frames
    return out


def compare(native_dir: Path, result_dir: Path, mode: str) -> BenchResult:
    """Per-frame segment error of ``result_dir`` against ``native_dir``."""
    pairs = pairing(native_dir)
    native_bones = list(pairs)
    ours_bones = [pairs[b] for b in native_bones]
    meshes = native_hand_meshes(native_dir)
    male = [m for m in meshes if "female" not in m.lower()] or meshes
    native = segment_centroids(native_dir, {male[0]}, native_bones)
    ours = segment_centroids(result_dir, {"hands"}, ours_bones)
    tips = np.array([b.endswith(("02.L", "02.R")) for b in ours_bones])
    result = BenchResult(weapon=Path(native_dir).name, mode=mode)
    every: list[np.ndarray] = []
    tip_all: list[np.ndarray] = []
    for name, frames in native.items():
        other = ours.get(name)
        if other is None or other.shape != frames.shape:
            continue
        err = np.linalg.norm(frames - other, axis=2)  # (frames, bones)
        valid = err[~np.isnan(err)]
        if not len(valid):
            continue
        tip_err = err[:, tips]
        tip_err = tip_err[~np.isnan(tip_err)]
        every.append(valid)
        tip_all.append(tip_err)
        result.sequences.append(SequenceError(
            name, frames.shape[0], float(valid.mean()), float(np.percentile(valid, 95)),
            float(valid.max()), float(tip_err.mean()) if len(tip_err) else float("nan")))
    if every:
        allv = np.concatenate(every)
        result.mean, result.p95, result.max = (float(allv.mean()),
                                               float(np.percentile(allv, 95)),
                                               float(allv.max()))
        tipv = np.concatenate(tip_all)
        result.tips_mean = float(tipv.mean()) if len(tipv) else float("nan")
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
    lines = ["| weapon | identity mean | round trip mean | round trip p95 | tips mean | "
             "worst sequence |", "|---|---|---|---|---|---|"]
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
                     f"{trip.worst_sequence if trip and not trip.error else ''} |")
    ok = [m["roundtrip"] for m in by_weapon.values()
          if "roundtrip" in m and not m["roundtrip"].error]
    if ok:
        lines.insert(0, f"Round trip over {len(ok)} weapons: mean "
                        f"{np.mean([r.mean for r in ok]):.3f} u, tips "
                        f"{np.nanmean([r.tips_mean for r in ok]):.3f} u\n")
    path.with_suffix(".md").write_text("\n".join(lines) + "\n")


__all__ = ["BenchResult", "compare", "is_native", "native_hand_meshes", "pairing",
           "run_weapon", "segment_centroids", "write_report"]
