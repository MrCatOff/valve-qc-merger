"""Check a map before it goes on a server: what would crash it, what is
missing, what players would miss.

:func:`check_map` reads one BSP and returns a :class:`MapReport` — counts,
precache slots, spawns, entity classes and a list of :class:`Issue` in
three levels:

- ``critical``: the map does not load or a client crashes (a model an
  entity names is in no folder, a texture in no WAD, a lit face wider than
  256 texels, geometry past ±4096, a Blue Shift map, too few spawns for a
  full server);
- ``warning``: it loads, but something is lost or close to a limit (spawns
  under the profile's count or in a wall, classes CS 1.6 drops, sounds not
  found, textures over 256, slots near 512, a name only a case-insensitive
  file system finds);
- ``info``: what the map is.

Every issue has a ``code``, the ``fix`` operation that cures it (or None)
and ``where`` (an entity index, a texture, a class). Files are looked up
the engine's way: the map's own game folder, then the folders given, then
``valve`` next to a mod folder.
"""

from __future__ import annotations

import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.geometry import Geometry, geometry
from valve_qc_merger.mapkit.hull import EMPTY, STAND, point_contents
from valve_qc_merger.mapkit.rules import RESOURCE_KEYS, Rules, known_class
from valve_qc_merger.mapkit.textures import (
    SPECIAL,
    WadIndex,
    broken_animations,
    find_wads,
    texture_uses,
)
from valve_qc_merger.server.bsp import wad_names
from valve_qc_merger.server.entities import parse
from valve_qc_merger.server.maps import SKY_SIDES, _detail_textures
from valve_qc_merger.server.scan import FolderInventory, scan_folder

LEVELS = ("info", "warning", "critical")
CT, T = "info_player_start", "info_player_deathmatch"
SPAWN_LIFT = 1.0  # CS spawns a player at the spot's origin + 1 unit up


@dataclass
class Issue:
    level: str
    code: str
    text: str
    where: dict[str, object] = field(default_factory=dict)
    fix: str | None = None


@dataclass
class MapReport:
    map: str
    file: str
    version: int = B.VERSION
    size: int = 0
    crc: str = ""
    bounds: dict[str, list[float]] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    slots: dict[str, int] = field(default_factory=dict)
    spawns: dict[str, int] = field(default_factory=dict)
    classes: dict[str, int] = field(default_factory=dict)
    wads: list[str] = field(default_factory=list)
    skyname: str = ""
    issues: list[Issue] = field(default_factory=list)

    def add(self, level: str, code: str, text: str, fix: str | None = None,
            **where: object) -> None:
        self.issues.append(Issue(level, code, text, dict(where), fix))

    @property
    def level(self) -> str:
        """The worst level of the issues ("ok" without warnings)."""
        worst = max((LEVELS.index(i.level) for i in self.issues), default=0)
        return LEVELS[worst] if worst else "ok"

    def of_level(self, level: str) -> list[Issue]:
        return [i for i in self.issues if i.level == level]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# -- folders ------------------------------------------------------------------

def game_folder(map_path: Path) -> Path:
    """The mod folder a map belongs to (the one above ``maps/``)."""
    parent = Path(map_path).resolve().parent
    return parent.parent if parent.name.lower() == "maps" else parent


def search_folders(map_path: Path, extra: list[Path] | None = None) -> list[Path]:
    """Where the engine finds a map's files: its game folder, ``extra``
    (each with its ``_downloads`` folder), then ``valve`` next to any of
    them."""
    out: list[Path] = []
    for folder in [game_folder(map_path), *(Path(p) for p in extra or [])]:
        for candidate in (folder, folder.with_name(folder.name + "_downloads")):
            if candidate.is_dir() and candidate.resolve() not in out:
                out.append(candidate.resolve())
    for folder in list(out):
        valve = folder.parent / "valve"
        if valve.is_dir() and valve.resolve() not in out:
            out.append(valve.resolve())
    return out


class Finder:
    """Case-aware lookup of game paths over several folders (scanned once)."""

    def __init__(self, folders: list[Path]) -> None:
        self.folders = folders
        self.inventories: list[FolderInventory] = [scan_folder(f) for f in folders]

    def find(self, relative: str) -> tuple[Path | None, bool]:
        """(the file, whether its name is spelled exactly) — a different
        case is found by Windows clients but not by a Linux server."""
        relative = relative.replace("\\", "/").lstrip("/")
        for inventory in self.inventories:
            actual = inventory.actual(relative)
            if actual is not None:
                return inventory.root / actual, actual == relative
        return None, False


_FINDERS: dict[tuple[Path, ...], Finder] = {}


def finder_for(folders: list[Path]) -> Finder:
    key = tuple(folders)
    if key not in _FINDERS:
        _FINDERS[key] = Finder(folders)
    return _FINDERS[key]


def forget(folder: Path | None = None) -> None:
    """Drop the scanned listings of ``folder`` (all with None): call after
    writing files into a game folder."""
    if folder is None:
        _FINDERS.clear()
        return
    target = Path(folder).resolve()
    for key in [k for k in _FINDERS if target in k]:
        del _FINDERS[key]


# -- resources ----------------------------------------------------------------

def entity_resources(entities: list[dict[str, str]]) -> tuple[dict[str, list[int]],
                                                               dict[str, list[int]]]:
    """(models and sprites, sounds) the entities name -> entity indices."""
    models: dict[str, list[int]] = {}
    sounds: dict[str, list[int]] = {}
    for index, entity in enumerate(entities):
        for key, value in entity.items():
            value = value.strip().replace("\\", "/")
            low = value.lower()
            if not value or value.startswith(("*", "!")) or key == "wad":
                continue
            if low.endswith((".mdl", ".spr")):
                models.setdefault(value, []).append(index)
            elif low.endswith(".wav") and (key in RESOURCE_KEYS or key.startswith("noise")):
                sounds.setdefault(value.removeprefix("sound/"), []).append(index)
    return models, sounds


def _model_version(path: Path) -> int | None:
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
    except OSError:
        return None
    if len(head) < 8 or head[:4] != b"IDST":
        return None
    version: int = struct.unpack_from("<i", head, 4)[0]
    return version


# -- the check ----------------------------------------------------------------

def check_map(path: Path, folders: list[Path] | None = None, rules: Rules | None = None,
              stock_models: int | None = None, stock_sounds: int | None = None) -> MapReport:
    """Check map ``path`` (see the module). ``folders``: extra game folders
    (a clean ``cstrike``); ``stock_*``: slots the game DLL itself takes
    (default: ReGameDLL's counts)."""
    from valve_qc_merger.server.stock import STOCK_MODELS, STOCK_SOUNDS

    path = Path(path)
    rules = rules or Rules()
    bsp = B.Bsp.read(path)
    geo = geometry(bsp)
    entities = parse(bsp.entity_text)
    finder = finder_for(search_folders(path, folders))
    report = MapReport(path.stem, str(path), size=len(bsp.source), crc=f"{bsp.crc():08x}")
    world = next((e for e in entities if e.get("classname") == "worldspawn"), {})
    report.wads = wad_names(world.get("wad", ""))
    report.skyname = world.get("skyname", "").strip()
    if bsp.blue_shift:
        report.add("critical", "blue_shift", "a Blue Shift map (first two lumps swapped): "
                   "Counter-Strike does not load it", "bsp.standard")

    _classes(report, entities)
    _bounds(report, geo, rules)
    _spawns(report, bsp, entities, rules)
    models, sounds = entity_resources(entities)
    _resources(report, finder, models, sounds)
    _textures(report, bsp, finder, rules)
    _sky_and_detail(report, path, finder)

    inline = max(len(geo.models) - 1, 0)
    report.slots = {"map": 1, "brush": inline, "models": sum(not m.lower().endswith(".spr")
                                                              for m in models),
                    "sprites": sum(m.lower().endswith(".spr") for m in models)}
    report.slots["total"] = 1 + inline + len(models)
    report.slots["sounds"] = len(sounds)
    stock_m = STOCK_MODELS if stock_models is None else stock_models
    stock_s = STOCK_SOUNDS if stock_sounds is None else stock_sounds
    report.slots["stock_models"], report.slots["stock_sounds"] = stock_m, stock_s
    free_models = rules.model_slots - stock_m - report.slots["total"]
    free_sounds = rules.sound_slots - stock_s - len(sounds)
    report.slots["free_models"], report.slots["free_sounds"] = free_models, free_sounds
    if free_models < 0:
        report.add("critical", "model_slots", f"{report.slots['total']} model slots + {stock_m} "
                   f"the game takes = {report.slots['total'] + stock_m} > {rules.model_slots}",
                   "props.merge")
    elif free_models < rules.model_slots * 0.1:
        report.add("warning", "model_slots", f"{report.slots['total']} model slots: "
                   f"{free_models} left of {rules.model_slots} for the server's models",
                   "props.merge")
    if free_sounds < rules.sound_slots * 0.1:
        report.add("warning", "sound_slots", f"{len(sounds)} sounds: {free_sounds} slots left "
                   f"of {rules.sound_slots}")

    report.counts = {"entities": len(entities), "brush_models": inline,
                     "textures": len(geo.textures), "faces": len(geo.faces),
                     "leafs": len(geo.leafs), "planes": len(geo.planes),
                     "clipnodes": len(geo.clipnodes), "vertices": len(geo.vertices),
                     "visibility": len(bsp.lumps[B.VISIBILITY]),
                     "lighting": len(bsp.lumps[B.LIGHTING])}
    if len(entities) > rules.edicts * rules.edicts_warn:
        report.add("warning", "entities", f"{len(entities)} entities of {rules.edicts} edicts "
                   f"({rules.max_players} players): little room for players' items")
    if len(geo.leafs) > rules.max_leafs:
        report.add("warning", "leafs", f"{len(geo.leafs)} leafs > {rules.max_leafs}")
    if not bsp.lumps[B.VISIBILITY]:
        report.add("warning", "no_vis", "no visibility data: the whole map is drawn everywhere "
                   "(low FPS)", "external.vis")
    if not bsp.lumps[B.LIGHTING]:
        report.add("warning", "no_light", "no lighting data: the map is drawn full bright")
    bad = geo.bad_extent_faces()
    if len(bad):
        report.add("critical", "bad_extents", f"{len(bad)} lit faces wider than 256 texels: "
                   "clients stop with \"Bad surface extents\"", "external.subdivide",
                   faces=[int(f) for f in bad[:50]])
    report.add("info", "summary", f"{len(entities)} entities, {len(geo.faces)} faces, "
               f"{len(geo.textures)} textures, {inline} brush models")
    return report


def _classes(report: MapReport, entities: list[dict[str, str]]) -> None:
    counts: dict[str, int] = {}
    for entity in entities:
        name = entity.get("classname", "")
        counts[name] = counts.get(name, 0) + 1
    report.classes = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    for name, count in report.classes.items():
        if name and not known_class(name):
            indices = [i for i, e in enumerate(entities) if e.get("classname") == name]
            report.add("warning", "unknown_class", f"{count} {name}: CS 1.6 drops them "
                       "(\"Can't init\")", "cso.remove", **{"class": name, "entities": indices})


def _bounds(report: MapReport, geo: Geometry, rules: Rules) -> None:
    if not len(geo.models):
        report.add("critical", "no_world", "the map has no world model")
        return
    mins, maxs = geo.bounds(0)
    report.bounds = {"mins": [round(float(v), 1) for v in mins],
                     "maxs": [round(float(v), 1) for v in maxs]}
    reach = float(max(np.abs(mins).max(), np.abs(maxs).max()))
    if reach > rules.world_limit:
        report.add("critical", "world_bounds", f"geometry reaches {reach:.0f} units, past "
                   f"±{rules.world_limit:.0f}: players there are lost", "external.move")
    elif reach >= rules.world_limit - rules.world_margin:
        report.add("warning", "world_bounds", f"geometry reaches {reach:.0f} units, within "
                   f"{rules.world_margin:.0f} of ±{rules.world_limit:.0f}")


def _origin(entity: dict[str, str]) -> tuple[float, float, float] | None:
    try:
        x, y, z = (float(v) for v in entity.get("origin", "").split()[:3])
    except ValueError:
        return None
    return x, y, z


def _spawns(report: MapReport, bsp: B.Bsp, entities: list[dict[str, str]],
            rules: Rules) -> None:
    spots = [(i, e) for i, e in enumerate(entities) if e.get("classname") in (CT, T)]
    report.spawns = {"ct": sum(e["classname"] == CT for _i, e in spots),
                     "t": sum(e["classname"] == T for _i, e in spots), "bad": 0}
    placed = [(i, o) for i, e in spots if (o := _origin(e)) is not None]
    if placed:
        points = np.array([o for _i, o in placed]) + [0.0, 0.0, SPAWN_LIFT]
        contents = point_contents(bsp, points, STAND)
        stuck = [placed[k][0] for k in np.nonzero(contents != EMPTY)[0]]
        report.spawns["bad"] = len(stuck)
        if stuck:
            report.add("warning", "spawn_in_wall", f"{len(stuck)} spawns where a standing "
                       "player does not fit (stuck at round start)", "spawn.fix",
                       entities=stuck)
    half = rules.max_players // 2
    for team, key in (("CT", "ct"), ("T", "t")):
        have = report.spawns[key]
        if have < half:
            report.add("critical", "few_spawns", f"{team}: {have} spawns for {half} players "
                       f"a team ({rules.max_players} on the server)", "spawn.generate",
                       team=key, have=have, want=rules.min_spawns)
        elif have < rules.min_spawns:
            report.add("warning", "few_spawns", f"{team}: {have} spawns, the {rules.profile} "
                       f"profile wants {rules.min_spawns}", "spawn.generate", team=key,
                       have=have, want=rules.min_spawns)


def _resources(report: MapReport, finder: Finder, models: dict[str, list[int]],
               sounds: dict[str, list[int]]) -> None:
    for name, indices in sorted(models.items(), key=lambda kv: kv[0].lower()):
        found, exact = finder.find(name)
        if found is None:
            report.add("critical", "missing_model", f"{name} not found (the server stops "
                       "loading the map)", "entity.remove", file=name, entities=indices)
            continue
        if not exact:
            report.add("warning", "case_mismatch", f"{name} is spelled "
                       f"{found.name} on disk: a Linux server does not find it", "entity.set",
                       file=name, actual=found.name, entities=indices)
        if name.lower().endswith(".mdl"):
            version = _model_version(found)
            if version is not None and version != 10:
                report.add("critical", "model_version", f"{name}: studio version {version} "
                           "(CS 1.6 loads only 10)", "model.convert", file=name)
    for name, indices in sorted(sounds.items(), key=lambda kv: kv[0].lower()):
        found, exact = finder.find(f"sound/{name}")
        if found is None:
            report.add("warning", "missing_sound", f"sound/{name} not found (plays nothing)",
                       "entity.remove", file=f"sound/{name}", entities=indices)
        elif not exact:
            report.add("warning", "case_mismatch", f"sound/{name} is spelled {found.name} on "
                       "disk: a Linux server does not find it", "entity.set",
                       file=f"sound/{name}", actual=found.name, entities=indices)


def _textures(report: MapReport, bsp: B.Bsp, finder: Finder, rules: Rules) -> None:
    paths = find_wads(report.wads, [inv.root for inv in finder.inventories])
    wads = {name: WadIndex.read(p) for name, p in paths.items() if p is not None}
    missing_wads = [name for name, p in paths.items() if p is None]
    uses = texture_uses(bsp, wads)
    for name in missing_wads:
        needed = [u.name for u in uses if not u.found]
        report.add("critical", "missing_wad", f"{name} not found"
                   + (f" ({len(needed)} textures are in no other WAD of the map)"
                      if needed else " (no texture needs it)"), "worldspawn.wad" if not needed
                   else "textures.embed", wad=name)
    lost = [u.name for u in uses if not u.found and not u.name.lower().startswith(SPECIAL)]
    if lost:
        text = (f"{len(lost)} textures are in none of the map's WADs"
                + (f" (missing: {', '.join(missing_wads)})" if missing_wads else "")
                + ": " + ", ".join(lost[:8]) + (" …" if len(lost) > 8 else ""))
        report.add("critical", "missing_texture", text, "textures.embed", textures=lost)
    for use in uses:
        side = max(use.width, use.height)
        if side > 2 * rules.texture_warn:
            report.add("critical", "texture_size", f"texture {use.name} is {use.width}x"
                       f"{use.height} (over {2 * rules.texture_warn})", "external.retexture",
                       texture=use.name)
        elif side > rules.texture_warn:
            report.add("warning", "texture_size", f"texture {use.name} is {use.width}x"
                       f"{use.height} (over {rules.texture_warn}: blurred or not drawn on "
                       "some clients)", "external.retexture", texture=use.name)
        if use.width % 16 or use.height % 16:
            report.add("warning", "texture_not_16", f"texture {use.name} is {use.width}x"
                       f"{use.height}: not a multiple of 16", None, texture=use.name)
    for name in broken_animations([u.name for u in uses]):
        report.add("warning", "texture_animation", f"animated texture {name}: a frame is "
                   "missing", None, texture=name)
    unused = [name for name, index in wads.items()
              if not any(u.wad == name for u in uses if not u.embedded)]
    for name in unused:
        report.add("info", "unused_wad", f"{name}: no texture of the map comes from it",
                   "textures.prune_wads", wad=name)


def _sky_and_detail(report: MapReport, path: Path, finder: Finder) -> None:
    if report.skyname:
        missing = [f"gfx/env/{report.skyname}{s}.tga" for s in SKY_SIDES
                   if finder.find(f"gfx/env/{report.skyname}{s}.tga")[0] is None]
        if missing:
            report.add("warning", "missing_sky", f"sky {report.skyname}: {len(missing)} of 6 "
                       "sides not found (black sky)", None, files=missing)
    detail, _exact = finder.find(f"maps/{path.stem}_detail.txt")
    if detail is not None:
        wanted = _detail_textures(detail.read_text(encoding="latin-1"))
        missing = [f for f in wanted if finder.find(f)[0] is None]
        if missing:
            report.add("warning", "missing_detail", f"{len(missing)} detail textures not "
                       "found", None, files=missing)


def map_wads(path: Path, bsp: B.Bsp, folders: list[Path] | None = None) -> list[WadIndex]:
    """The WADs map ``path`` lists that are found, in its order."""
    world = next((e for e in parse(bsp.entity_text) if e.get("classname") == "worldspawn"), {})
    found = find_wads(wad_names(world.get("wad", "")), search_folders(path, folders))
    return [WadIndex.read(p) for p in found.values() if p is not None]


def marked_entities(report: MapReport) -> list[int]:
    """Entities the report's critical issues and warnings point at."""
    out: set[int] = set()
    for issue in report.issues:
        if issue.level != "info" and issue.code != "unknown_class":
            indices = issue.where.get("entities", [])
            if isinstance(indices, list):
                out.update(int(i) for i in indices)
    return sorted(out)


def check_maps(paths: list[Path], folders: list[Path] | None = None,
               rules: Rules | None = None) -> list[MapReport | Exception]:
    out: list[MapReport | Exception] = []
    for path in paths:
        try:
            out.append(check_map(path, folders, rules))
        except (B.BspError, OSError) as exc:
            out.append(exc)
    return out


__all__ = ["Finder", "Issue", "LEVELS", "MapReport", "check_map", "check_maps",
           "entity_resources", "forget", "game_folder", "map_wads", "marked_entities",
           "search_folders"]
