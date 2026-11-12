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
from collections.abc import Callable
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
    group: str = ""  # props_groups.json: models of a group go into one part, side by side
    folder: str = ""  # the work folder / manifest name (the key, after its group)
    variant_of: str = ""  # the same geometry as this model's folder: one of its skins
    skin: int = 0


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
                reporter: Reporter | None = None, verify: bool = True,
                groups: dict[str, list[str]] | None = None,
                variants: bool = True) -> PropsResult:
    """Steps 3-7 of the module. ``out``: the game folder the merged models
    go to (``models/<folder>/``, default the first map's name); the plans
    rewrite each map's entities (save them with :func:`fix.save_plan`).
    ``groups``: group name -> model paths that go into one part side by side
    (``props_groups.json``); ``variants``: models with the same geometry and
    other textures become skins of one model (``$texturegroup``)."""
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
        _assign_groups(result.models, groups or {})
        decompiled: dict[str, Path] = {}
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
            decompiled[prop.folder] = qcs[0].parent
        if variants:
            _find_variants([p for p in result.models if p.folder in decompiled], decompiled)
        for prop in result.models:
            if prop.folder not in decompiled or prop.reason:
                continue
            labels[prop.folder] = _sequence_labels(next(decompiled[prop.folder].glob("*.qc")))
            if not prop.variant_of:
                shutil.copytree(decompiled[prop.folder], sources / prop.folder)
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
            rep = prop.variant_of or prop.folder
            if not prop.reason and rep not in manifest:
                prop.reason = "merge-props left it apart (too big or broken)"
        if not manifest:
            return _finish(result, maps)
        _add_skins(result.models, manifest, run.outputs, decompiled, merged, log)
        built: dict[str, str] = {}  # part file name -> game path
        for qc in run.outputs:
            mdl = qc.with_suffix(".mdl")
            if studiomdl is not None:
                done = run_compile(CompileOptions(qc, Path(studiomdl)), reporter)
                if done.exit_code or not done.outputs:
                    result.log.append(f"studiomdl failed for {qc.name}")
                    for key, entry in manifest.items():
                        if entry.get("model") == f"{qc.stem}.mdl":
                            for prop in result.models:
                                if key in (prop.folder, prop.variant_of):
                                    prop.reason = "the merged part does not compile"
                    continue
                mdl = done.outputs[0]
            if mdl.exists() and verify:
                members = {k: sources / k for k, e in manifest.items()
                           if e.get("model") == f"{qc.stem}.mdl" and (sources / k).is_dir()}
                for key, error in verify_part(mdl, members, manifest, labels).items():
                    if error > PLACE_TOLERANCE:
                        for prop in result.models:
                            if key not in (prop.folder, prop.variant_of):
                                continue
                            prop.reason = (f"moved {error:.2f} units in the merged model"
                                           if error != float("inf") else "no vertices in the "
                                           "merged model")
                            result.log.append(f"{prop.key}: {prop.reason} — left as it was")
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
            from valve_qc_merger.mapkit.analyze import forget
            forget(out)
        for prop in result.models:
            if prop.reason:
                continue
            entry = manifest[prop.variant_of or prop.folder]
            part = built.get(str(entry.get("model", "")))
            if part is None:
                prop.reason = prop.reason or "its part was not built"
                continue
            prop_labels = labels.get(prop.variant_of or prop.folder, [])
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
                if prop.skin:
                    plan.add("entity.set", f"#{index} skin: {prop.skin} ({prop.path} is a skin "
                             f"of {prop.variant_of})", index, key="skin", value=str(prop.skin))
        (out / f"models/{folder}").mkdir(parents=True, exist_ok=True)
        (out / f"models/{folder}/{name}.json").write_text(json.dumps(
            {"parts": result.parts, "models": {p.path: {"part": built.get(
                str(manifest.get(p.variant_of or p.folder, {}).get("model", ""))),
                **manifest.get(p.variant_of or p.folder, {}), "skin": p.skin}
                for p in result.merged}}, indent=1), encoding="utf-8")
    finally:
        if keep_work is None:
            shutil.rmtree(work, ignore_errors=True)
    return _finish(result, maps)


PLACE_TOLERANCE = 0.05  # units: a merged model's first frame against its original


def read_groups(path: Path) -> dict[str, list[str]]:
    """``props_groups.json``: ``{"vehicles": ["models/car_red.mdl", …], …}``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {str(k): [str(m) for m in v] for k, v in data.items() if isinstance(v, list)}


def _assign_groups(models: list[PropModel], groups: dict[str, list[str]]) -> None:
    """Folder names that sort a group's models together (merge-props fills
    its parts in that order); the rest after the groups."""
    member = {m.replace("\\", "/").lower(): (i, g) for i, (g, ms) in enumerate(groups.items())
              for m in ms}
    for prop in models:
        found = member.get(prop.path.lower())
        if found is not None:
            prop.group = found[1]
            prop.folder = f"g{found[0]:02d}_{prop.key}"
        else:
            prop.folder = prop.key


_NODE_NAME = re.compile(r'"[^"]*"')


def _smd_digest(text: str, digest: object, materials: list[str] | None) -> None:
    """Feed an SMD to ``digest`` without its bone names and texture names
    (two models of one geometry whose bones are named apart still match);
    the textures of its triangles go to ``materials``."""
    section, count = "", 0
    for line in text.splitlines():
        word = line.strip()
        if word in ("nodes", "skeleton", "triangles") and section in ("", "done"):
            section, count = word, 0
            digest.update(word.encode())  # type: ignore[attr-defined]
            continue
        if word == "end" and section:
            section = "done"
            continue
        if section == "nodes":
            line = _NODE_NAME.sub('""', line)
        elif section == "triangles":
            if count % 4 == 0:
                if materials is not None:
                    materials.append(word)
                count += 1
                continue
            count += 1
        digest.update(line.encode("latin-1"))  # type: ignore[attr-defined]


def _triangles(directory: Path) -> tuple[str, list[str]]:
    """(a hash of a decompiled model's geometry, skeleton and animations
    without bone and texture names, the texture of every triangle in order)."""
    digest = hashlib.sha1()
    materials: list[str] = []
    qc = next(directory.glob("*.qc"), None)
    order = re.findall(r'(?im)^\s*(?:\$body\s+"?[^"\s]+"?|studio)\s+"?([^"\s]+)"?',
                       qc.read_text(encoding="latin-1")) if qc else []
    smds = [directory / f"{o.removesuffix('.smd')}.smd" for o in order]
    smds = [p for p in smds if p.is_file()] or sorted(directory.glob("*.smd"))
    for smd in smds:
        _smd_digest(smd.read_text(encoding="latin-1"), digest, materials)
    anims = directory / "anims"
    for anim in sorted(anims.glob("*.smd")) if anims.is_dir() else []:
        _smd_digest(anim.read_text(encoding="latin-1"), digest, None)
    return digest.hexdigest(), materials


def _texture_digest(directory: Path, materials: list[str]) -> str:
    files = {p.name.lower(): p for p in directory.rglob("*") if p.is_file()}
    digest = hashlib.sha1()
    for name in materials:
        found = files.get(name.lower())
        digest.update(found.read_bytes() if found else name.encode())
    return digest.hexdigest()


def _find_variants(models: list[PropModel], decompiled: dict[str, Path]) -> None:
    """Models with the same geometry, skeleton and animations as another:
    with the same textures too a duplicate (its body, skin 0), with other
    textures a skin of the first one."""
    first: dict[str, tuple[PropModel, str]] = {}
    skins: dict[str, dict[str, int]] = {}
    for prop in sorted(models, key=lambda m: m.folder):
        signature, materials = _triangles(decompiled[prop.folder])
        if not materials:
            continue
        look = _texture_digest(decompiled[prop.folder], materials)
        known = first.get(signature)
        if known is None:
            first[signature] = (prop, look)
            skins[prop.folder] = {look: 0}
            continue
        rep, _rep_look = known
        looks = skins[rep.folder]
        if look not in looks:
            looks[look] = len(looks)
        prop.variant_of, prop.skin = rep.folder, looks[look]


def _add_skins(models: list[PropModel], manifest: dict[str, dict[str, object]],
               outputs: list[Path], decompiled: dict[str, Path], merged: Path,
               log: Callable[[str], None]) -> None:
    """Give each merged part the skins of its models' variants: the
    variants' textures copied in, a ``$texturegroup`` row per skin."""
    from valve_qc_merger.merge_player.merger import parse_texturegroups
    by_rep: dict[str, list[PropModel]] = {}
    for prop in models:
        if prop.variant_of and prop.skin and not prop.reason:
            by_rep.setdefault(prop.variant_of, []).append(prop)
    for qc in outputs:
        reps = [r for r in by_rep if manifest.get(r, {}).get("model") == f"{qc.stem}.mdl"]
        if not reps:
            continue
        text = qc.read_text(encoding="latin-1")
        if parse_texturegroups(text):
            for rep in reps:
                for prop in by_rep[rep]:
                    prop.reason = "its model already has skins"
            continue
        columns: list[str] = []
        rows: dict[int, dict[str, str]] = {}
        for rep in reps:
            merged_smd = next((p for p in (merged / rep, qc.parent / rep)
                               if (p / "weapon.smd").is_file()), None)
            if merged_smd is None:
                continue
            _sig, base = _triangles_of(merged_smd / "weapon.smd")
            for prop in sorted(by_rep[rep], key=lambda m: m.skin):
                mine = _merged_materials(decompiled[prop.folder])
                if len(mine) != len(base):
                    prop.reason = "its triangles do not line up with its look-alike's"
                    continue
                mapping: dict[str, str] = {}
                for old, new in zip(base, mine, strict=True):
                    if mapping.setdefault(old, new) != new:
                        mapping = {}
                        break
                if not mapping:
                    prop.reason = "its textures do not map onto its look-alike's"
                    continue
                for old, new in mapping.items():
                    source = next((p for p in decompiled[prop.folder].rglob("*")
                                   if p.name.lower() == new.lower()), None)
                    if source is None:
                        continue
                    target = qc.parent / new
                    if target.exists() and target.read_bytes() != source.read_bytes():
                        target = qc.parent / f"{prop.folder}_{new}"
                    shutil.copy2(source, target)
                    if old not in columns:
                        columns.append(old)
                    rows.setdefault(prop.skin, {})[old] = target.name
        if not columns or not rows:
            continue
        lines = ["$texturegroup skinfamilies", "{",
                 "\t{ " + " ".join(f'"{c}"' for c in columns) + " }"]
        for skin in range(1, max(rows) + 1):
            row = rows.get(skin, {})
            lines.append("\t{ " + " ".join(f'"{row.get(c, c)}"' for c in columns) + " }")
        lines.append("}")
        masked = set(re.findall(r'(?im)^\s*\$texrendermode\s+"?([^"\s]+)"?\s+masked', text))
        extra = [f'$texrendermode "{row[c]}" masked' for row in rows.values() for c in row
                 if c in masked]
        anchor = re.search(r"(?im)^\s*\$sequence\b", text)
        at = anchor.start() if anchor else len(text)
        qc.write_text(text[:at] + "\n".join(extra + lines) + "\n\n" + text[at:],
                      encoding="latin-1")
        log(f"{qc.stem}: {max(rows)} skins for {', '.join(reps)}")


def _merged_materials(directory: Path) -> list[str]:
    """The textures of a model's triangles in the order merge-props joins
    its submodels (the ones it takes, in its order)."""
    from valve_qc_merger.merge_view.discovery import load_model
    from valve_qc_merger.services.merge_props import prop_parts
    stems = [stem for item in prop_parts(load_model(directory, require_anims=False))
             .weapon_stems for stem in ([item] if isinstance(item, str) else item)]
    out: list[str] = []
    for stem in stems:
        smd = directory / f"{stem}.smd"
        if smd.is_file():
            out += _triangles_of(smd)[1]
    return out


def _triangles_of(smd: Path) -> tuple[str, list[str]]:
    """(the file, the texture of each triangle in order) — triangles with
    three vertices at one point left out (merge-props adds those as
    attachment carriers)."""
    materials: list[str] = []
    inside, block = False, list[str]()
    for line in smd.read_text(encoding="latin-1").splitlines():
        word = line.strip()
        if word == "triangles":
            inside, block = True, []
            continue
        if inside and word == "end":
            inside = False
            continue
        if inside:
            block.append(word)
            if len(block) == 4:
                points = {tuple(v.split()[1:4]) for v in block[1:]}
                if len(points) > 1:
                    materials.append(block[0])
                block = []
    return str(smd), materials


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
           "find_studiomdl", "read_groups", "stale_parts", "unused_models", "verify_part"]
