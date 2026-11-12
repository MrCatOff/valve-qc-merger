"""A map's fixes as a plan: shown first, applied to a copy, never over an
original that players already have under another CRC.

A :class:`FixPlan` is a list of :class:`Operation` (what, on which entity,
in words) built from a check report by :func:`plan_fixes` (or by hand, or
by Studio); nothing is written until :func:`save_plan`. Each operation says

- ``crc_safe`` — it changes only the entity lump (the map CRC stays);
- ``client_visible`` — players must see it although the CRC stays (a
  worldspawn change the client reads from its own copy of the map).

A plan with any operation that is not CRC-safe, or a client-visible one, is
saved under a new name (``<map>_sp``, then ``_sp2`` …) with the map's
companion files (``.res``, ``.ent``, ``maps/<map>.txt``, detail textures
list, overview), and the mapcycle given is pointed at it. Otherwise the map
keeps its name: the BSP is rewritten (the original kept as ``.bak``), or a
``maps/<map>.ent`` is written for ReHLDS (``sv_use_entity_file 1``) and the
BSP is left alone.

Operation codes: ``entity.remove``, ``entity.set``, ``entity.unset``,
``entity.add``, ``entity.replace``, ``cso.remove``, ``cso.replace``,
``spawn.fix``, ``spawn.generate``, ``worldspawn.wad``, ``file.copy`` (a file
the map needs from a source folder) and ``res.write``.
"""

from __future__ import annotations

import copy
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.analyze import MapReport, game_folder, search_folders
from valve_qc_merger.mapkit.rules import Rules
from valve_qc_merger.mapkit.spawns import SpawnOptions, fix_stuck, generate, sample_spots
from valve_qc_merger.mapkit.textures import WadIndex
from valve_qc_merger.server.bsp import wad_names
from valve_qc_merger.server.entities import entity_text, parse

NEW_NAME_SUFFIX = "_sp"
TRANSLUCENT = {"1", "2", "3", "5"}  # rendermodes a func_illusionary keeps the look of


@dataclass
class Operation:
    code: str
    text: str
    entity: int | None = None  # index in the map's entity list as read
    data: dict[str, object] = field(default_factory=dict)
    crc_safe: bool = True
    client_visible: bool = False
    enabled: bool = True


@dataclass
class FixPlan:
    map_path: Path
    operations: list[Operation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, code: str, text: str, entity: int | None = None, *, crc_safe: bool = True,
            client_visible: bool = False, **data: object) -> Operation:
        op = Operation(code, text, entity, dict(data), crc_safe, client_visible)
        self.operations.append(op)
        return op

    @property
    def active(self) -> list[Operation]:
        return [op for op in self.operations if op.enabled]

    @property
    def crc_safe(self) -> bool:
        return all(op.crc_safe for op in self.active)

    @property
    def needs_new_name(self) -> bool:
        return any(not op.crc_safe or op.client_visible for op in self.active)

    def apply(self, entities: list[dict[str, str]]) -> list[dict[str, str]]:
        """The entity list with every enabled operation applied (a copy;
        indices refer to ``entities`` as given)."""
        out: list[dict[str, str] | None] = [dict(e) for e in entities]
        added: list[dict[str, str]] = []
        for op in self.active:
            index = op.entity if op.entity is not None and 0 <= op.entity < len(out) else -1
            target = out[index] if index >= 0 else None
            if op.code in ("entity.remove", "cso.remove"):
                if target is not None and target.get("classname") != "worldspawn":
                    out[index] = None
            elif op.code == "entity.set" and target is not None:
                target[str(op.data["key"])] = str(op.data["value"])
            elif op.code == "entity.unset" and target is not None:
                target.pop(str(op.data["key"]), None)
            elif op.code in ("entity.replace", "cso.replace") and target is not None:
                keep = {k: v for k, v in target.items() if k in ("origin", "angles", "model",
                                                                 "targetname", "rendermode",
                                                                 "renderamt", "rendercolor")}
                extra = op.data.get("extra", {})
                out[index] = {"classname": str(op.data["classname"]), **keep,
                                  **(extra if isinstance(extra, dict) else {})}
            elif op.code == "spawn.fix" and target is not None:
                origin = op.data.get("origin")
                if isinstance(origin, (tuple, list)) and len(origin) == 3:
                    target["origin"] = " ".join(f"{float(v):g}" for v in origin)
                else:
                    out[index] = None
            elif op.code in ("entity.add", "spawn.generate"):
                new = op.data.get("new")
                if isinstance(new, dict):
                    added.append({str(k): str(v) for k, v in new.items()})
            elif op.code == "worldspawn.wad":
                world = next((e for e in out if e is not None
                              and e.get("classname") == "worldspawn"), None)
                if world is not None:
                    world["wad"] = str(op.data["value"])
        return [e for e in out if e is not None] + added


# -- building a plan ----------------------------------------------------------

@dataclass
class FixOptions:
    spawns: int | None = None  # spawns a team (None: the rules' min_spawns; 0: none added)
    spawn_options: SpawnOptions = field(default_factory=SpawnOptions)
    cso_entities: str = "remove"  # or "keep"
    remove_missing: bool = True  # entities naming a file found nowhere
    sources: list[Path] = field(default_factory=list)  # where missing files are copied from
    markers: list[tuple[str, int]] = field(default_factory=list)  # info_target name, count
    wads: bool = True  # worldspawn's wad: bare names, the unused ones dropped
    entity_rules: dict[str, dict[str, object]] = field(default_factory=dict)


def _find_in(folders: list[Path], relative: str) -> Path | None:
    relative = relative.replace("\\", "/").lstrip("/").lower()
    for folder in folders:
        candidate = folder / relative
        if candidate.is_file():
            return candidate
        # case-insensitive walk (CSO dumps mix cases)
        current = folder
        for part in relative.split("/"):
            if not current.is_dir():
                current = None  # type: ignore[assignment]
                break
            match = next((p for p in current.iterdir() if p.name.lower() == part), None)
            if match is None:
                current = None  # type: ignore[assignment]
                break
            current = match
        if current is not None and current.is_file():
            return current
    return None


def plan_fixes(report: MapReport, options: FixOptions | None = None,
               rules: Rules | None = None) -> FixPlan:
    """The operations that cure ``report``'s issues (see the module)."""
    options = options or FixOptions()
    rules = rules or Rules()
    path = Path(report.file)
    bsp = B.Bsp.read(path)
    entities = parse(bsp.entity_text)
    plan = FixPlan(path)
    configured = rules.extra.get("entity_rules")
    entity_rules: dict[str, dict[str, object]] = dict(
        options.entity_rules or (configured if isinstance(configured, dict) else {}))
    gone: set[int] = set()
    for issue in report.issues:
        where = issue.where
        listed = where.get("entities", [])
        indices = [int(i) for i in listed] if isinstance(listed, list) else []
        if issue.code == "unknown_class" and options.cso_entities == "remove":
            name = str(where.get("class", ""))
            rule = entity_rules.get(name) or entity_rules.get("*unknown*") or {}
            action = str(rule.get("action", "remove"))
            keep_brush = bool(rule.get("keep_if_brush", True))
            for index in indices:
                entity = entities[index]
                if action == "keep":
                    continue
                if keep_brush and entity.get("model", "").startswith("*"):
                    see_through = entity.get("rendermode", "0") in TRANSLUCENT
                    cls = "func_illusionary" if see_through else "func_wall"
                    plan.add("cso.replace", f"{name} #{index} -> {cls} (keeps its brushes)",
                             index, classname=cls, extra={})
                elif action == "replace" and rule.get("classname"):
                    plan.add("cso.replace", f"{name} #{index} -> {rule['classname']}", index,
                             classname=str(rule["classname"]), extra=rule.get("extra", {}))
                else:
                    plan.add("cso.remove", f"remove {name} #{index} (CS 1.6 drops it)", index)
                    gone.add(index)
        elif issue.code in ("missing_model", "missing_sound"):
            file = str(where.get("file", ""))
            source = _find_in(options.sources, file) if options.sources else None
            if source is not None:
                plan.add("file.copy", f"copy {file} from {source.parent}", None,
                         source=str(source), target=file)
            elif issue.code == "missing_model" and options.remove_missing:
                for index in indices:
                    if index not in gone:
                        plan.add("entity.remove", f"remove {entities[index].get('classname')} "
                                 f"#{index}: {file} is nowhere", index)
                        gone.add(index)
        elif issue.code == "case_mismatch":
            file = str(where.get("file", ""))
            actual = str(where.get("actual", ""))
            fixed = file.rsplit("/", 1)[0] + "/" + actual if "/" in file else actual
            for index in indices:
                for key, value in entities[index].items():
                    plain = value.replace("\\", "/").removeprefix("sound/")
                    if plain.lower() == file.removeprefix("sound/").lower():
                        new = fixed.removeprefix("sound/") if not value.startswith("sound/") \
                            else fixed
                        plan.add("entity.set", f"#{index} {key}: {value} -> {new}", index,
                                 key=key, value=new)
        elif issue.code == "spawn_in_wall":
            for index in indices:
                origin = tuple(float(v) for v in entities[index]["origin"].split()[:3])
                moved = fix_stuck(bsp, origin)  # type: ignore[arg-type]
                if moved is None:
                    plan.add("spawn.fix", f"remove spawn #{index}: no room within 32 units",
                             index, origin=None)
                    gone.add(index)
                else:
                    plan.add("spawn.fix", f"move spawn #{index} to {moved[0]:g} {moved[1]:g} "
                             f"{moved[2]:g}", index, origin=moved)
    wanted = rules.min_spawns if options.spawns is None else options.spawns
    if wanted:
        current = plan.apply(entities)
        spawn_opts = copy.copy(options.spawn_options)
        spawn_opts.per_team = wanted
        result = generate(bsp, current, spawn_opts)
        for placed in result.added:
            team = "CT" if placed.classname == "info_player_start" else "T"
            plan.add("spawn.generate", f"add a {team} spawn at {placed.origin[0]:g} "
                     f"{placed.origin[1]:g} {placed.origin[2]:g}", None, new=placed.entity())
        plan.notes += result.notes
    for name, count in options.markers:
        current = plan.apply(entities)
        taken = [_xyz(e["origin"]) for e in current
                 if e.get("classname") == "info_target" and "origin" in e]
        result = sample_spots(bsp, current, count, options.spawn_options,
                              [t for t in taken if t is not None])
        for placed in result.added:
            entity = {"classname": "info_target", "targetname": name,
                      "origin": placed.entity()["origin"]}
            plan.add("entity.add", f"add marker {name} at {entity['origin']}", None,
                     new=entity)
        plan.notes += [f"{name}: {n}" for n in result.notes]
    if options.wads:
        world = next((e for e in entities if e.get("classname") == "worldspawn"), {})
        value = world.get("wad", "")
        unused = {str(i.where.get("wad", "")).lower() for i in report.issues
                  if i.code == "unused_wad"}
        names = [n for n in wad_names(value) if n.lower() not in unused]
        tidy = ";".join(names) + (";" if names else "")
        if value and tidy != value:
            plan.add("worldspawn.wad", f"wad: {len(wad_names(value))} -> {len(names)} WADs, "
                     "bare names" + (f" (unused: {', '.join(sorted(unused))})" if unused else ""),
                     None, value=tidy)
    return plan


def _xyz(text: str) -> np.ndarray | None:
    try:
        return np.array([float(v) for v in text.split()[:3]], np.float64)
    except ValueError:
        return None


# -- texture plans --------------------------------------------------------------

def plan_replace_texture(path: Path, name: str, image: Path,
                         folders: list[Path] | None = None) -> FixPlan:
    """Replace texture ``name`` of map ``path`` with a picture (resized to the
    texture's size). An embedded one changes the BSP (a new map name); an
    external one is replaced in a copy of its WAD under a new name the map's
    ``wad`` then lists (players must see it: a new map name too)."""
    from valve_qc_merger.mapkit.analyze import map_wads
    from valve_qc_merger.mapkit.geometry import geometry
    from valve_qc_merger.mapkit.images import read_image
    from valve_qc_merger.mapkit.textures import texture_from_image

    bsp = B.Bsp.read(path)
    tex = next((t for t in geometry(bsp).textures
                if t.offset >= 0 and t.name.lower() == name.lower()), None)
    if tex is None:
        raise KeyError(f"{path.stem} has no texture {name}")
    texture = texture_from_image(tex.name, read_image(image), tex.width, tex.height)
    plan = FixPlan(path)
    if tex.embedded:
        plan.add("textures.replace", f"replace embedded {tex.name} ({tex.width}x{tex.height}) "
                 f"with {image.name}", None, crc_safe=False, name=tex.name, texture=texture)
        return plan
    wad = next((w for w in map_wads(path, bsp, folders) if tex.name.lower() in w.entries), None)
    if wad is None:
        raise KeyError(f"{tex.name} is in none of the map's WADs")
    new_wad = f"{wad.path.stem}_{path.stem}.wad"
    plan.add("textures.wad_copy", f"{tex.name} replaced with {image.name} in {new_wad} (a copy "
             f"of {wad.path.name})", None, client_visible=True, source=str(wad.path),
             wad=new_wad, name=tex.name, texture=texture)
    world = next((e for e in parse(bsp.entity_text) if e.get("classname") == "worldspawn"), {})
    names = [new_wad if n.lower() == wad.path.name.lower() else n
             for n in wad_names(world.get("wad", ""))]
    plan.add("worldspawn.wad", f"wad: {wad.path.name} -> {new_wad}", None, client_visible=True,
             value=";".join(names) + ";")
    return plan


def animation_frames(path: Path, name: str) -> list[str]:
    """The frames of animated texture ``name`` (``+0lava`` or ``lava``) in
    order: ``+0lava``, ``+1lava`` …"""
    from valve_qc_merger.mapkit.geometry import geometry
    base = name[2:] if name.startswith("+") and len(name) > 2 else name
    names = {t.name.lower(): t.name for t in geometry(B.Bsp.read(path)).textures
             if t.offset >= 0}
    frames = []
    for k in range(10):
        found = names.get(f"+{k}{base}".lower())
        if found is None:
            break
        frames.append(found)
    return frames


def frame_images(image: Path) -> list[Path]:
    """``fire_0.png`` -> ``fire_0.png``, ``fire_1.png`` … as long as they exist."""
    stem = image.stem
    if not stem[-2:-1] == "_" or not stem[-1:].isdigit():
        return [image]
    head = stem[:-1]
    out = []
    for k in range(int(stem[-1]), 10):
        candidate = image.with_name(f"{head}{k}{image.suffix}")
        if not candidate.is_file():
            break
        out.append(candidate)
    return out


def plan_replace_textures(path: Path, pairs: list[tuple[str, Path]],
                          folders: list[Path] | None = None) -> FixPlan:
    """Several textures at once (see :func:`plan_replace_texture`); an
    animated one (``+0name``, or a set of pictures ``name_0.png``,
    ``name_1.png`` …) is every frame in turn. External textures of one WAD
    land in one copy of it."""
    plan = FixPlan(path)
    expanded: list[tuple[str, Path]] = []
    for name, image in pairs:
        frames = animation_frames(path, name)
        images = frame_images(image)
        if frames and (len(images) > 1 or name.startswith("+")):
            if len(images) < len(frames):
                plan.notes.append(f"{frames[0]}: {len(frames)} frames, {len(images)} pictures "
                                  "— the last frames keep theirs")
            expanded += list(zip(frames, images, strict=False))
        else:
            expanded.append((name, image))
    seen_wad: set[str] = set()
    for name, image in expanded:
        one = plan_replace_texture(path, name, image, folders)
        for op in one.operations:
            if op.code == "worldspawn.wad":
                if op.data["value"] in seen_wad:
                    continue
                seen_wad.add(str(op.data["value"]))
            plan.operations.append(op)
    return plan


def plan_embed(path: Path, names: list[str] | None = None, missing_only: bool = False,
               folders: list[Path] | None = None) -> FixPlan:
    """Embed external textures (all, ``names``, or only those whose WAD a
    player may not have) — the map then needs fewer WADs."""
    from valve_qc_merger.mapkit.analyze import map_wads
    from valve_qc_merger.mapkit.geometry import geometry

    bsp = B.Bsp.read(path)
    wads = map_wads(path, bsp, folders)
    external = [t for t in geometry(bsp).textures if t.offset >= 0 and not t.embedded]
    if names:
        wanted = {n.lower() for n in names}
        external = [t for t in external if t.name.lower() in wanted]
    elif missing_only:
        stock = {w.path.name.lower() for w in wads if w.path.parent.name.lower() == "valve"}
        external = [t for t in external
                    if not any(t.name.lower() in w.entries for w in wads
                               if w.path.name.lower() in stock)]
    found = [t for t in external if any(t.name.lower() in w.entries for w in wads)]
    plan = FixPlan(path)
    if not found:
        plan.notes.append("no external texture to embed (found in the map's WADs)")
        return plan
    plan.add("textures.embed", f"embed {len(found)} textures from the map's WADs", None,
             crc_safe=False, names=[t.name for t in found])
    still = {t.name.lower() for t in geometry(bsp).textures
             if t.offset >= 0 and not t.embedded} - {t.name.lower() for t in found}
    world = next((e for e in parse(bsp.entity_text) if e.get("classname") == "worldspawn"), {})
    keep = [w.path.name for w in wads if still & set(w.entries)]
    listed = wad_names(world.get("wad", ""))
    kept = [n for n in listed if n.lower() in {k.lower() for k in keep}
            or not any(w.path.name.lower() == n.lower() for w in wads)]
    if kept != listed:
        plan.add("worldspawn.wad", f"wad: {len(listed)} -> {len(kept)} WADs (the rest are "
                 "embedded)", None, value=";".join(kept) + (";" if kept else ""))
    return plan


def plan_extract(path: Path, wad: str, strip: bool = False,
                 names: list[str] | None = None) -> FixPlan:
    """Write the embedded textures to ``wad`` (in the game folder); with
    ``strip`` the map keeps only their names and lists the WAD."""
    bsp = B.Bsp.read(path)
    plan = FixPlan(path)
    plan.add("textures.extract", f"write the embedded textures to {wad}", None, wad=wad,
             names=names)
    if strip:
        plan.add("textures.strip", "the map keeps only their names", None, crc_safe=False,
                 names=names)
        world = next((e for e in parse(bsp.entity_text) if e.get("classname") == "worldspawn"),
                     {})
        listed = wad_names(world.get("wad", ""))
        if wad.lower() not in {n.lower() for n in listed}:
            plan.add("worldspawn.wad", f"wad: + {wad}", None, client_visible=True,
                     value=";".join([*listed, wad]) + ";")
    return plan


# -- saving -------------------------------------------------------------------

@dataclass
class SaveResult:
    map_name: str
    written: list[Path] = field(default_factory=list)
    new_name: bool = False
    crc_before: str = ""
    crc_after: str = ""
    notes: list[str] = field(default_factory=list)


def next_name(maps_dir: Path, name: str) -> str:
    """``<map>_sp``, or ``_sp2``, ``_sp3`` … when taken (a map already
    ``…_spN`` counts on from N)."""
    base, number = name, 1
    for suffix in (NEW_NAME_SUFFIX,):
        if suffix in name:
            head, _, tail = name.rpartition(suffix)
            if tail == "" or tail.isdigit():
                base, number = head, int(tail or 1)
    while True:
        candidate = f"{base}{NEW_NAME_SUFFIX}{number if number > 1 else ''}"
        if candidate != name and not (maps_dir / f"{candidate}.bsp").exists():
            return candidate
        number += 1


def _companions(game: Path, old: str) -> list[tuple[Path, str]]:
    """(file, its path relative to the game folder with ``{name}`` for the
    map name) of the map's own companion files."""
    out = []
    for rel in (f"maps/{old}.txt", f"maps/{old}_detail.txt", f"maps/{old}.ent",
                f"overviews/{old}.txt", f"overviews/{old}.bmp", f"overviews/{old}.tga"):
        path = game / rel
        if path.is_file():
            out.append((path, rel.replace(old, "{name}", 1)))
    return out


def save_plan(plan: FixPlan, *, out_dir: Path | None = None, ent: bool = False,
              res: bool = True, folders: list[Path] | None = None,
              mapcycle: Path | None = None, backup: bool = True) -> SaveResult:
    """Apply ``plan`` and write the map (see the module). ``out_dir``: a
    game folder to write into instead of the map's own; ``backup``: keep
    ``<map>.bsp.bak`` when the map is written over."""
    source = plan.map_path
    game = game_folder(source)
    target_game = Path(out_dir) if out_dir else game
    maps = target_game / "maps"
    maps.mkdir(parents=True, exist_ok=True)
    bsp = B.Bsp.read(source)
    entities = plan.apply(parse(bsp.entity_text))
    result = SaveResult(source.stem, crc_before=f"{bsp.crc():08x}")
    new_name = plan.needs_new_name
    if new_name:
        result.map_name = next_name(maps, source.stem)
        result.new_name = True
        if ent:
            result.notes.append("a new map name: the BSP is written (not a .ent)")
            ent = False
    for op in plan.active:
        if op.code == "file.copy":
            dest = target_game / str(op.data["target"]).replace("\\", "/").lower()
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy2(str(op.data["source"]), dest)
                result.written.append(dest)
    _write_wads(plan, bsp, target_game, result)
    if any(op.code in LUMP_OPS for op in plan.active) and ent:
        result.notes.append("textures change the BSP: the BSP is written (not a .ent)")
        ent = False
    name = result.map_name
    if ent:
        path = maps / f"{name}.ent"
        path.write_bytes(entity_text(entities).encode("latin-1"))
        result.written.append(path)
        if out_dir and not (maps / f"{name}.bsp").exists():
            shutil.copy2(source, maps / f"{name}.bsp")
            result.written.append(maps / f"{name}.bsp")
        result.crc_after = result.crc_before
    else:
        original = B.Bsp.parse(bsp.write(), bsp.name)
        wads = _wads_of(source, original, folders) if any(
            op.code == "textures.embed" for op in plan.active) else []
        bsp.set_entity_text(entity_text(entities))
        _apply_lump_ops(plan, bsp, wads, result)
        target = maps / f"{name}.bsp"
        if backup and target.exists() and target.resolve() == source.resolve():
            kept = source.with_name(source.name + ".bak")
            if not kept.exists():
                shutil.copy2(source, kept)
                result.written.append(kept)
        target.write_bytes(bsp.write())
        result.written.append(target)
        result.crc_after = f"{bsp.crc():08x}"
        reread = B.Bsp.read(target)
        touched = {B.ENTITIES} | ({B.TEXTURES} if any(op.code in LUMP_OPS
                                                      for op in plan.active) else set())
        moved = [B.LUMP_NAMES[i] for i in range(B.LUMPS)
                 if i not in touched and reread.lumps[i] != original.lumps[i]]
        if moved:  # pragma: no cover - the writer copies other lumps byte for byte
            result.notes.append(f"lumps changed unexpectedly: {', '.join(moved)}")
    if new_name or out_dir:
        for path, rel in _companions(game, source.stem):
            dest = target_game / rel.format(name=name)
            if dest.resolve() != path.resolve() and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
                result.written.append(dest)
    from valve_qc_merger.mapkit.analyze import forget
    forget(target_game)
    if res:
        res_path = _write_res(maps / f"{name}.bsp", target_game, [game, *(folders or [])])
        if res_path is not None:
            result.written.append(res_path)
    if mapcycle is not None and new_name and mapcycle.is_file():
        lines = mapcycle.read_text(encoding="latin-1").splitlines()
        swapped = [name if line.split("//", 1)[0].strip() == source.stem else line
                   for line in lines]
        if swapped != lines:
            mapcycle.write_text("\n".join(swapped) + "\n", encoding="latin-1")
            result.written.append(mapcycle)
    return result


LUMP_OPS = ("textures.replace", "textures.embed", "textures.strip")


def _wads_of(path: Path, bsp: B.Bsp, folders: list[Path] | None) -> list[WadIndex]:
    """The WADs the map lists as read (before its worldspawn is changed)."""
    from valve_qc_merger.mapkit.analyze import map_wads
    return map_wads(path, bsp, folders)


def _apply_lump_ops(plan: FixPlan, bsp: B.Bsp, wads: list[WadIndex],
                    result: SaveResult) -> None:
    """Texture operations that rewrite the textures lump, in plan order."""
    from valve_qc_merger.mapkit import textures as tx
    for op in plan.active:
        if op.code == "textures.replace":
            tx.replace_embedded(bsp, str(op.data["name"]), op.data["texture"])  # type: ignore[arg-type]
        elif op.code == "textures.embed":
            names = op.data.get("names")
            done = tx.embed(bsp, wads, list(names) if isinstance(names, list) else None)
            result.notes.append(f"embedded {len(done)} textures")
        elif op.code == "textures.strip":
            names = op.data.get("names")
            tx.extract(bsp, list(names) if isinstance(names, list) else None, strip=True)


def _write_wads(plan: FixPlan, bsp: B.Bsp, target_game: Path, result: SaveResult) -> None:
    """Texture operations that write a WAD next to the map's game folder."""
    from valve_qc_merger.mapkit import textures as tx
    from valve_qc_merger.sprite.wad import Texture, miptex_lump, write_wad
    copies: dict[tuple[str, str], dict[str, object]] = {}
    for op in plan.active:
        if op.code == "textures.extract":
            names = op.data.get("names")
            textures = tx.extract(bsp, list(names) if isinstance(names, list) else None)
            path = target_game / str(op.data["wad"])
            path.write_bytes(write_wad(textures))
            result.written.append(path)
        elif op.code == "textures.wad_copy" and isinstance(op.data.get("texture"), Texture):
            target = (str(op.data["source"]), str(op.data["wad"]))
            copies.setdefault(target, {})[str(op.data["name"]).lower()] = op.data["texture"]
    for (source_path, wad), replaced in copies.items():
        # every replacement in one WAD goes into one copy of it
        source = WadIndex.read(Path(source_path))
        lumps = []
        with open(source.path, "rb") as handle:
            for entry, (offset, size) in sorted(source.entries.items(),
                                                key=lambda kv: kv[1][0]):
                handle.seek(offset)
                raw = handle.read(size)
                name = raw[:16].split(b"\0", 1)[0].decode("latin-1") or entry
                new = replaced.get(entry)
                if isinstance(new, Texture):
                    new.name = name
                    raw = miptex_lump(new)
                lumps.append((name, raw))
        path = target_game / wad
        path.write_bytes(tx.wad_of_lumps(lumps))
        result.written.append(path)


def _write_res(map_path: Path, game: Path, folders: list[Path] | None) -> Path | None:
    if not map_path.exists():
        return None
    from valve_qc_merger.server.maps import check_map as check_res
    from valve_qc_merger.server.maps import write_res
    from valve_qc_merger.server.scan import scan_folder
    inventory = scan_folder(game)
    fallback = [scan_folder(f) for f in search_folders(map_path, folders) if f != game.resolve()]
    return write_res(check_res(map_path, inventory, fallback), map_path)


__all__ = ["FixOptions", "FixPlan", "NEW_NAME_SUFFIX", "Operation", "SaveResult", "next_name",
           "animation_frames", "frame_images", "plan_embed", "plan_extract", "plan_fixes",
           "plan_replace_texture", "plan_replace_textures", "save_plan"]
