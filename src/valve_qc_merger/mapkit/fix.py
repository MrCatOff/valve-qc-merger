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
              mapcycle: Path | None = None) -> SaveResult:
    """Apply ``plan`` and write the map (see the module). ``out_dir``: a
    game folder to write into instead of the map's own."""
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
        bsp.set_entity_text(entity_text(entities))
        target = maps / f"{name}.bsp"
        if target.exists() and target.resolve() == source.resolve():
            backup = source.with_name(source.name + ".bak")
            if not backup.exists():
                shutil.copy2(source, backup)
                result.written.append(backup)
        target.write_bytes(bsp.write())
        result.written.append(target)
        result.crc_after = f"{bsp.crc():08x}"
    if new_name or out_dir:
        for path, rel in _companions(game, source.stem):
            dest = target_game / rel.format(name=name)
            if dest.resolve() != path.resolve() and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
                result.written.append(dest)
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
           "plan_fixes", "save_plan"]
