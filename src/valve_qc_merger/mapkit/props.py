"""A map's prop models merged into one or a few models (``merge-props``):
the map then takes one or two model slots instead of fifteen to thirty.

1. **collect** — entities of the prop classes (``cycler_sprite`` and
   ``env_sprite`` naming a ``.mdl``) by model, with the ``body`` / ``skin`` /
   ``sequence`` they use. ``cycler`` is left alone: ReGameDLL stops its
   animation when ``sequence`` is not 0, and a merged model renumbers the
   sequences;
2. **set apart** — a model an entity shows with ``body`` or ``skin`` other
   than 0, a model not found, one ``merge-props`` ships standalone: the
   report says why;
3. **decompile** each into a work folder (in process, CSO v9 too), named
   after its path (``models/props/trash.mdl`` -> ``props_trash``) so two
   ``trash.mdl`` in two folders stay two;
4. **merge** with ``merge-props`` (its limits: 32 submodels a group, 2048
   vertices, 127 bones, the texture budget, 255 sequences, pev_body < 256);
5. **compile** each part with studiomdl;
6. **name** each part ``models/<map>/<name>_<hash8>.mdl`` — eight characters
   of the file's hash, so a changed set of props is a new file and clients
   that cached the old one download it (a client never re-downloads a file
   of the same name);
7. **rewrite** the entities: ``model`` the part, ``body`` the model's
   pev_body, ``sequence`` its sequence's index in the part; ``rendermode``,
   ``scale``, ``angles`` … stay. Only the entity lump changes: the CRC stays.

``--shared NAME`` merges the props of several maps into one set (each map
takes as many slots as parts it uses).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.analyze import Finder, finder_for, search_folders
from valve_qc_merger.mapkit.fix import FixPlan
from valve_qc_merger.server.entities import parse
from valve_qc_merger.services.base import CallbackReporter, Reporter

PROP_CLASSES = ("cycler_sprite", "env_sprite")
_SEQUENCE = re.compile(r'^\s*\$sequence\s+"?([^"\s]+)"?', re.MULTILINE | re.IGNORECASE)


@dataclass
class PropModel:
    path: str  # as the entities name it (models/props/trash.mdl)
    key: str  # the work folder / manifest name (props_trash)
    file: Path | None = None
    uses: list[tuple[int, int]] = field(default_factory=list)  # (map number, entity index)
    sequences: set[int] = field(default_factory=set)
    reason: str = ""  # why it stays as it is ("" = merged)


@dataclass
class PropsResult:
    models: list[PropModel] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)  # game paths of the merged models
    plans: list[FixPlan] = field(default_factory=list)  # one per map
    slots_before: int = 0
    slots_after: int = 0
    log: list[str] = field(default_factory=list)
    out: Path | None = None

    @property
    def merged(self) -> list[PropModel]:
        return [m for m in self.models if not m.reason]

    @property
    def apart(self) -> list[PropModel]:
        return [m for m in self.models if m.reason]


def model_key(path: str) -> str:
    """``models/props/trash.mdl`` -> ``props_trash`` (a folder / manifest name)."""
    rel = path.replace("\\", "/").lower().removeprefix("models/").removesuffix(".mdl")
    return re.sub(r"[^a-z0-9_]+", "_", rel).strip("_") or "model"


def collect(maps: list[Path], folders: list[Path] | None = None,
            classes: tuple[str, ...] = PROP_CLASSES) -> list[PropModel]:
    """Step 1 and 2 of the module over one or several maps."""
    models: dict[str, PropModel] = {}
    for number, path in enumerate(maps):
        finder: Finder = finder_for(search_folders(path, folders))
        entities = parse(B.Bsp.read(path).entity_text)
        for index, entity in enumerate(entities):
            model = entity.get("model", "").replace("\\", "/")
            if entity.get("classname") not in classes or not model.lower().endswith(".mdl"):
                continue
            key = model.lower()
            prop = models.get(key)
            if prop is None:
                found, _exact = finder.find(model)
                prop = models[key] = PropModel(model, model_key(model), found)
            prop.uses.append((number, index))
            try:
                prop.sequences.add(int(float(entity.get("sequence", "0") or 0)))
                body = int(float(entity.get("body", "0") or 0))
                skin = int(float(entity.get("skin", "0") or 0))
            except ValueError:
                body = skin = 0
            if body or skin:
                prop.reason = prop.reason or f"shown with body {body} / skin {skin}"
    taken: dict[str, int] = {}
    for prop in models.values():
        if prop.file is None:
            prop.reason = prop.reason or "not found"
        count = taken.get(prop.key, 0)
        taken[prop.key] = count + 1
        if count:
            prop.key = f"{prop.key}_{count + 1}"
    return sorted(models.values(), key=lambda m: m.key)


def _sequence_labels(qc: Path) -> list[str]:
    return _SEQUENCE.findall(qc.read_text(encoding="latin-1"))


def merge_props(maps: list[Path], out: Path, name: str, *, folders: list[Path] | None = None,
                studiomdl: Path | None = None, classes: tuple[str, ...] = PROP_CLASSES,
                folder: str | None = None, keep_work: Path | None = None,
                reporter: Reporter | None = None, verify: bool = True) -> PropsResult:
    """Steps 3-7 of the module. ``out``: the game folder the merged models
    go to (``models/<folder>/``, default the first map's name); the plans
    rewrite each map's entities (save them with :func:`fix.save_plan`)."""
    from valve_qc_merger.services.compile import CompileOptions, run_compile
    from valve_qc_merger.services.decompile import DecompileOptions, run_decompile
    from valve_qc_merger.services.merge_props import MergePropsOptions, run_merge_props

    result = PropsResult(out=out)
    result.models = collect(maps, folders, classes)
    folder = folder or maps[0].stem
    sink = reporter

    def log(message: str) -> None:
        result.log.append(message)
        if sink is not None:
            sink.log(message)

    reporter = CallbackReporter(on_log=log, cancel=getattr(sink, "cancel_event", None))
    work = Path(keep_work) if keep_work else Path(tempfile.mkdtemp(prefix="vqm_props_"))
    try:
        sources = work / "models"
        sources.mkdir(parents=True, exist_ok=True)
        labels: dict[str, list[str]] = {}
        for prop in result.models:
            if prop.reason or prop.file is None:
                continue
            target = work / "decompiled" / prop.key
            reporter.check()
            done = run_decompile(DecompileOptions(prop.file, target), CallbackReporter())
            qcs = sorted(target.rglob("*.qc"))
            if done.exit_code or not qcs:
                prop.reason = "does not decompile"
                continue
            dest = sources / prop.key
            shutil.copytree(qcs[0].parent, dest)
            labels[prop.key] = _sequence_labels(next(dest.glob("*.qc")))
        if not labels:
            result.log.append("no prop model to merge")
            return _finish(result, maps)
        merged = work / "merged"
        run = run_merge_props(MergePropsOptions(sources, merged, name=name,
                                                manifest_format="json"), reporter)
        manifest = dict(run.manifest)
        for failure in run.failures:
            result.log.append(f"merge: {failure}")
        for prop in result.models:
            if not prop.reason and prop.key not in manifest:
                prop.reason = "merge-props left it apart (too big or broken)"
        if not manifest:
            return _finish(result, maps)
        built: dict[str, str] = {}  # part file name -> game path
        for qc in run.outputs:
            mdl = qc.with_suffix(".mdl")
            if studiomdl is not None:
                done = run_compile(CompileOptions(qc, Path(studiomdl)), reporter)
                if done.exit_code or not done.outputs:
                    result.log.append(f"studiomdl failed for {qc.name}")
                    for key, entry in manifest.items():
                        if entry.get("model") == f"{qc.stem}.mdl":
                            next(p for p in result.models if p.key == key).reason = \
                                "the merged part does not compile"
                    continue
                mdl = done.outputs[0]
            if mdl.exists() and verify:
                members = {k: sources / k for k, e in manifest.items()
                           if e.get("model") == f"{qc.stem}.mdl" and (sources / k).is_dir()}
                for key, error in verify_part(mdl, members, manifest, labels).items():
                    if error > PLACE_TOLERANCE:
                        prop = next(p for p in result.models if p.key == key)
                        prop.reason = (f"moved {error:.2f} units in the merged model"
                                       if error != float("inf") else "no vertices in the "
                                       "merged model")
                        result.log.append(f"{key}: {prop.reason} — left as it was")
            digest = hashlib.sha1(mdl.read_bytes() if mdl.exists()
                                  else qc.read_bytes()).hexdigest()[:8]
            game_path = f"models/{folder}/{qc.stem}_{digest}.mdl"
            dest = out / game_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            if mdl.exists():
                shutil.copy2(mdl, dest)
            else:
                result.log.append(f"{qc.name}: not compiled (no studiomdl) — the QC is in "
                                  f"{qc.parent}")
                shutil.copytree(qc.parent, dest.with_suffix(""), dirs_exist_ok=True)
            built[f"{qc.stem}.mdl"] = game_path
            result.parts.append(game_path)
        for prop in result.models:
            if prop.reason:
                continue
            entry = manifest[prop.key]
            part = built.get(str(entry.get("model", "")))
            if part is None:
                prop.reason = prop.reason or "its part was not built"
                continue
            prop_labels = labels.get(prop.key, [])
            for number, index in prop.uses:
                plan = _plan_for(result, maps, number)
                ent = parse(B.Bsp.read(maps[number]).entity_text)[index]
                try:
                    old = int(float(ent.get("sequence", "0") or 0))
                except ValueError:
                    old = 0
                label = prop_labels[old] if 0 <= old < len(prop_labels) else None
                new_seq = entry.get(f"anim_{label}") if label else None
                plan.add("entity.set", f"#{index} model: {prop.path} -> {part}", index,
                         key="model", value=part)
                plan.add("entity.set", f"#{index} body: {entry.get('pev_body', 0)}", index,
                         key="body", value=str(entry.get("pev_body", 0)))
                if new_seq is not None and str(new_seq) != str(old):
                    plan.add("entity.set", f"#{index} sequence: {old} -> {new_seq}", index,
                             key="sequence", value=str(new_seq))
        (out / f"models/{folder}").mkdir(parents=True, exist_ok=True)
        (out / f"models/{folder}/{name}.json").write_text(json.dumps(
            {"parts": result.parts, "models": {p.path: {"part": built.get(
                str(manifest.get(p.key, {}).get("model", ""))), **manifest.get(p.key, {})}
                for p in result.merged}}, indent=1), encoding="utf-8")
    finally:
        if keep_work is None:
            shutil.rmtree(work, ignore_errors=True)
    return _finish(result, maps)


PLACE_TOLERANCE = 0.05  # units: a merged model's first frame against its original


def _frame0(directory: Path, body: int | None, sequence: int) -> np.ndarray:
    """Vertices of the first frame of ``sequence``: of body value ``body``,
    or (None) of the submodels merge-props takes from the model."""
    from valve_qc_merger.studio.scene import build_scene
    scene = build_scene(directory)
    pos, quats = scene.local_pose(sequence, 0)
    rot, trans = scene.world(pos, quats)
    if body is None:
        from valve_qc_merger.merge_view.discovery import load_model
        from valve_qc_merger.services.merge_props import prop_parts
        stems = {stem for item in prop_parts(load_model(directory, require_anims=False))
                 .weapon_stems for stem in ([item] if isinstance(item, str) else item)}
        batches = [b for b in scene.batches if b.stem in stems]
    else:
        batches = scene.visible_batches(scene.decode_body(body))
    points = [scene.skin(b, rot, trans)[0] for b in batches]
    return np.concatenate(points) if points else np.zeros((0, 3))


def _hausdorff(a: np.ndarray, b: np.ndarray) -> float:
    """The largest distance from a vertex of either set to the other set."""
    if not len(a) or not len(b):
        return float("inf")
    worst = 0.0
    for x, y in ((a, b), (b, a)):
        for start in range(0, len(x), 1024):
            d = np.linalg.norm(x[start:start + 1024, None] - y[None], axis=2).min(axis=1)
            worst = max(worst, float(d.max()))
    return worst


def verify_part(mdl: Path, originals: dict[str, Path], manifest: dict[str, dict[str, object]],
                labels: dict[str, list[str]]) -> dict[str, float]:
    """Each merged model's first frame in the compiled part against its own
    decompiled original: the largest distance of a vertex to the other's
    (units; inf when one has none)."""
    from valve_qc_merger.services.decompile import DecompileOptions, run_decompile
    out: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix="vqm_check_") as tmp:
        run_decompile(DecompileOptions(mdl, Path(tmp)), CallbackReporter())
        part = next((d for d in Path(tmp).iterdir() if d.is_dir()), None)
        if part is None:
            return {key: float("inf") for key in originals}
        for key, source in originals.items():
            entry = manifest[key]
            first = labels.get(key, [""])[0] if labels.get(key) else ""
            sequence = int(entry.get(f"anim_{first}", 0))  # type: ignore[call-overload]
            a = _frame0(source, None, 0)
            b = _frame0(part, int(entry.get("pev_body", 0)), sequence)  # type: ignore[call-overload]
            out[key] = _hausdorff(a, b)
    return out


def _plan_for(result: PropsResult, maps: list[Path], number: int) -> FixPlan:
    while len(result.plans) < len(maps):
        result.plans.append(FixPlan(maps[len(result.plans)]))
    return result.plans[number]


def _finish(result: PropsResult, maps: list[Path]) -> PropsResult:
    for number in range(len(maps)):
        _plan_for(result, maps, number)
    used_before = {m.path.lower() for m in result.models}
    result.slots_before = len(used_before)
    merged = {m.path.lower() for m in result.merged}
    result.slots_after = len(used_before - merged) + len(result.parts)
    return result


def unused_models(result: PropsResult, game: Path) -> list[str]:
    """Merged models no other map of ``game/maps`` names any more (they
    could be deleted from the server)."""
    merged = {m.path.lower() for m in result.merged}
    done = {p.resolve() for p in (plan.map_path for plan in result.plans)}
    still: set[str] = set()
    for path in sorted((game / "maps").glob("*.bsp")):
        if path.resolve() in done:
            continue
        try:
            text = B.Bsp.read(path).entity_text.lower().replace("\\", "/")
        except B.BspError:
            continue
        still |= {m for m in merged if m in text}
    return sorted(merged - still)


def find_studiomdl(given: Path | None = None) -> Path | None:
    """studiomdl: given, ``VQM_STUDIOMDL``, on ``PATH``, or the one built
    into this repository (``tools/studiomdl``)."""
    import os
    candidates = [given, os.environ.get("VQM_STUDIOMDL"), shutil.which("studiomdl"),
                  shutil.which("studiomdl.exe")]
    from valve_qc_merger.resources import data_root
    candidates += [data_root() / "tools" / "studiomdl", data_root() / "tools" / "studiomdl.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def stale_parts(game: Path, folder: str, name: str) -> list[Path]:
    """Merged parts ``models/<folder>/<name>*_<hash>.mdl`` no map of
    ``game/maps`` names any more."""
    parts = sorted((game / "models" / folder).glob(f"{name}*_????????.mdl"))
    texts = []
    for path in sorted((game / "maps").glob("*.bsp")):
        try:
            texts.append(B.Bsp.read(path).entity_text.lower().replace("\\", "/"))
        except B.BspError:
            continue
    for path in sorted((game / "maps").glob("*.ent")):
        texts.append(path.read_text(encoding="latin-1").lower())
    joined = "\n".join(texts)
    return [p for p in parts if f"models/{folder}/{p.name}".lower() not in joined]


__all__ = ["PROP_CLASSES", "PropModel", "PropsResult", "collect", "merge_props", "model_key",
           "find_studiomdl", "stale_parts", "unused_models", "verify_part"]
