"""The precache slots a map takes with this project's builds deployed.

A slot count is a sum of parts the studio knows — the map (brush models,
entity models/sprites/sounds), the project's compiled builds and the sounds
their models play — plus what the user says the game DLL and plugins
precache (ReGameDLL's own weapons and players, every AMXX plugin: the studio
cannot see those). Merging pays off here: a build turns N weapon models into
a few parts, and :func:`project_load` counts the slots that saved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from valve_qc_merger.server.bsp import MapResources
from valve_qc_merger.server.limits import LIMITS, level

if TYPE_CHECKING:
    from valve_qc_merger.project.model import Project

_CLIENT_SOUND = re.compile(r"\b5004\s+-?\d+\s+\"([^\"]+)\"")


@dataclass
class BuildLoad:
    name: str
    inputs: int  # assets merged
    outputs: list[str]  # game paths of the compiled models

    @property
    def saved(self) -> int:
        return max(self.inputs - len(self.outputs), 0) if self.outputs else 0


@dataclass
class ProjectLoad:
    builds: list[BuildLoad] = field(default_factory=list)
    client_sounds: set[str] = field(default_factory=set)  # relative to sound/
    sprites: list[str] = field(default_factory=list)  # effect sprites: model slots
    hud_files: list[str] = field(default_factory=list)  # weapon HUD txt + sheets: generic

    @property
    def models(self) -> list[str]:
        return sorted({m for b in self.builds for m in b.outputs})

    @property
    def saved(self) -> int:
        return sum(b.saved for b in self.builds)


def project_load(project: Project) -> ProjectLoad:
    """The models each run build deploys and the client sounds they play."""
    import json

    from valve_qc_merger.project.model import DEFAULT_DEPLOY_DIR, DEPLOY_DIRS
    load = ProjectLoad()
    for name, build in sorted(project.builds.items()):
        record = project.build_dir(name) / "last_run.json"
        if not record.exists():
            continue
        try:
            outputs = json.loads(record.read_text(encoding="utf-8")).get("outputs", [])
            inputs = len(project.build_assets(build))
        except (OSError, ValueError, KeyError):
            continue
        folder = (build.deploy_dir or DEPLOY_DIRS.get(build.kind, DEFAULT_DEPLOY_DIR))
        folder = folder.strip("/\\").replace("\\", "/")
        paths = []
        for output in outputs:
            qc = project.root / output
            if not output.lower().endswith(".qc"):
                continue
            stem = Path(output).stem
            if build.kind == "merge-players":
                paths.append(f"{folder}/{stem}/{stem}.mdl")
            else:
                paths.append(f"{folder}/{stem}.mdl")
            try:
                text = qc.read_text(encoding="latin-1")
            except OSError:
                continue
            for sound in _CLIENT_SOUND.findall(text):
                load.client_sounds.add(sound.replace("\\", "/").lower())
        load.builds.append(BuildLoad(name, inputs, paths))
    from valve_qc_merger.project import sprites as library
    load.sprites = library.effect_sprites(project)
    load.hud_files = sorted(library.hud_files(project))
    return load


@dataclass
class MergeRow:
    """One line of the as-imported / after-merge comparison: a build, or the
    models of a category that no build takes."""

    title: str
    imported: int  # model slots of its weapons as they are (one per weapon)
    merged: int  # after the merge (``imported`` when nothing merges them)
    state: str  # "run" | "planned" | "not run" | "no build"
    category: str = ""
    left_out: list[str] = field(default_factory=list)  # weapons the merge rejected
    parts: int = 0  # the merged parts alone (``merged`` minus what was left out)

    @property
    def saved(self) -> int:
        return self.imported - self.merged


@dataclass
class MergeComparison:
    rows: list[MergeRow] = field(default_factory=list)
    sounds_imported: int = 0  # sounds the imported models play
    sounds_merged: int = 0  # ... the merged models and the models left as they are

    @property
    def imported(self) -> int:
        return sum(r.imported for r in self.rows)

    @property
    def merged(self) -> int:
        return sum(r.merged for r in self.rows)

    @property
    def unmerged(self) -> int:
        """Slots of the models no build takes (in ``merged`` as they are)."""
        return sum(r.merged for r in self.rows if r.state == "no build")

    @property
    def left_out(self) -> int:
        """Slots of the models the merges left out (in ``merged`` as they are)."""
        return sum(r.merged - r.parts for r in self.rows if r.left_out)

    @property
    def pending(self) -> list[str]:
        return [r.title for r in self.rows if r.state == "not run"]


def merge_comparison(project: Project, load: ProjectLoad | None = None) -> MergeComparison:
    """The project's models as imported (every weapon a slot of its own; a
    model and its swap-hands copy are one weapon) against after the merge:
    each build's parts — from its last run, else from its plan, else (not run
    yet) as imported — plus the models no build takes, as they are."""
    import json

    from valve_qc_merger.project.model import ProjectError
    from valve_qc_merger.project.qc_edit import qc_file
    load = load or project_load(project)
    run = {b.name: b for b in load.builds if b.outputs}
    out = MergeComparison()
    covered: set[tuple[str, str]] = set()

    def key(name: str) -> tuple[str, str]:
        asset = project.assets[name]
        return asset.kind, project._origin(asset)

    for name, build in sorted(project.builds.items()):
        try:
            weapons = {key(a.name) for a in project.build_assets(build)}
        except ProjectError:
            continue
        if not weapons:
            continue
        covered |= weapons
        members = {a.name: key(a.name) for a in project.build_assets(build)}
        inside: set[str] | None = None  # assets the merge took (None: unknown)
        if name in run:
            merged, state = len(run[name].outputs), "run"
            inside = _manifest_names(project.build_dir(name) / "output")
        else:
            plan = project.build_dir(name) / "plan.json"
            parts = None
            if plan.is_file():
                try:
                    parts = json.loads(plan.read_text(encoding="utf-8")).get("parts")
                except (OSError, ValueError):
                    parts = None
            if parts:
                merged, state = len(parts), "planned"
                listed = {m for part in parts for m in part.get("models", [])}
                inside = listed or None
            else:
                merged, state = len(weapons), "not run"
        parts_only = merged
        left_out: list[str] = []
        if inside is not None:
            # a manifest names what the merge took — maybe a swap-hands copy
            taken = {key(n) for n in inside if n in project.assets}
            left_out = sorted({n for n, w in members.items() if w not in taken},
                              key=str.lower)
            merged += len({members[n] for n in left_out})  # they stay as they are
        out.rows.append(MergeRow(f"{name} ({build.kind})", len(weapons), merged, state,
                                 build.category or "", left_out, parts_only))
    loose: dict[str, set[tuple[str, str]]] = {}
    for name, asset in project.assets.items():
        weapon = key(name)
        if weapon not in covered:
            loose.setdefault(asset.category, set()).add(weapon)
    for category in sorted(loose, key=lambda c: (c == "", c.lower())):
        count = len(loose[category])
        out.rows.append(MergeRow(category or "Uncategorized", count, count, "no build",
                                 category))
    played: dict[str, set[str]] = {}  # view-model client sounds (event 5004)
    for name in project.assets:
        try:
            text = qc_file(project.asset_dir(name)).read_text(encoding="latin-1")
        except (OSError, ValueError):
            continue
        played[name] = {s.replace("\\", "/").lower() for s in _CLIENT_SOUND.findall(text)}
    out.sounds_imported = len({s.lower() for sounds in played.values() for s in sounds})
    rejected = {key(n) for r in out.rows for n in r.left_out}
    loose_names = {n for n in project.assets if key(n) not in covered or key(n) in rejected}
    out.sounds_merged = len(load.client_sounds | {s.lower() for n in loose_names
                                                  for s in played.get(n, ())})
    return out


def _manifest_names(output: Path) -> set[str] | None:
    """The models a build's manifests list (their section / key names);
    None when the build wrote none."""
    import configparser
    import json
    import tomllib

    from valve_qc_merger.project.model import MANIFEST_SUFFIXES
    names: set[str] = set()
    found = False
    for path in sorted(Path(output).glob("*")) if Path(output).is_dir() else []:
        if path.suffix.lower() not in MANIFEST_SUFFIXES or path.name == "inventory.json":
            continue
        try:
            if path.suffix.lower() == ".ini":
                parser = configparser.ConfigParser(interpolation=None)
                parser.optionxform = str
                parser.read(path, encoding="utf-8")
                names.update(parser.sections())
            else:
                text = path.read_text(encoding="utf-8")
                data = json.loads(text) if path.suffix.lower() == ".json" \
                    else tomllib.loads(text)
                if isinstance(data, dict) and isinstance(data.get("models"), dict):
                    data = data["models"]
                if isinstance(data, dict):
                    names.update(k for k, v in data.items() if isinstance(v, dict))
            found = True
        except (OSError, ValueError, configparser.Error):
            continue
    return names if found else None


@dataclass
class BudgetLine:
    key: str
    title: str
    limit: int
    parts: list[tuple[str, int]]

    @property
    def used(self) -> int:
        return sum(count for _label, count in self.parts)

    @property
    def level(self) -> str:
        return level(self.used, self.limit)


def budget(map_resources: MapResources | None, load: ProjectLoad, *,
           extra: dict[str, int] | None = None,
           client_sounds_as: str = "generic", unprecached: int = 0,
           stock_models: int = 0, stock_sounds: int = 0, unmerged: int = 0,
           left_out: int = 0,
           as_imported: int | None = None,
           client_sounds: int | None = None) -> dict[str, BudgetLine]:
    """Slots per kind (models / sounds / generic) for one map ("no map": the
    project and the extras alone). ``client_sounds_as``: where the plugin
    precaches the view models' client sounds — ``generic`` (ReHLDS: 4096
    slots, nothing else uses them) or ``sound``. ``stock_models``: the
    stock CS models the game DLL precaches (weapons, shields, players,
    shells, gibs, items, effect sprites — :data:`.stock.STOCK_MODELS`) and
    ``stock_sounds`` the sounds it precaches (:data:`.stock.STOCK_SOUNDS`).
    ``unmerged``: project models no build takes (they stay as they are);
    ``left_out``: models a build's merge rejected (they stay as they are);
    ``as_imported``: count the project's models as imported instead (no
    merge: every weapon a slot) — the before of before/after.
    ``client_sounds``: the client sound count to use (default: the builds')."""
    extra = extra or {}
    parts: dict[str, list[tuple[str, int]]] = {"models": [], "sounds": [], "generic": []}
    if map_resources is not None:
        parts["models"] += [("map brush models (world included)", map_resources.brush_models),
                            ("map entity models / sprites", len(map_resources.models))]
        parts["sounds"].append(("map entity sounds", len(map_resources.sounds)))
    if as_imported is None:
        parts["models"].append(("project builds", len(load.models)))
        if unmerged:
            parts["models"].append(("project models in no build (as imported)", unmerged))
        if left_out:
            parts["models"].append(("models the merges left out (as imported)", left_out))
    else:
        parts["models"].append(("project models as imported", as_imported))
    parts["models"].append(("project sprites", len(load.sprites)))
    parts["generic"].append(("weapon HUD files", len(load.hud_files)))
    target = "sounds" if client_sounds_as == "sound" else "generic"
    parts[target].append(("view-model client sounds",
                          len(load.client_sounds) if client_sounds is None
                          else int(client_sounds)))
    if stock_models:
        parts["models"].append(("ReGameDLL: weapons, players, items, effect sprites",
                                int(stock_models)))
    if stock_sounds:
        parts["sounds"].append(("ReGameDLL: weapons, players, radio, impacts",
                                int(stock_sounds)))
    for key in parts:
        parts[key].append(("other game DLL + plugins (your estimate)",
                           int(extra.get(key, 0))))
    if unprecached:
        parts["models"].append(("unprecached stock models", -int(unprecached)))
    return {lim.key: BudgetLine(lim.key, lim.title, lim.limit, parts[lim.key])
            for lim in LIMITS}


def map_files(game_dir: Path | None) -> list[Path]:
    if not game_dir or not (Path(game_dir) / "maps").is_dir():
        return []
    return sorted((Path(game_dir) / "maps").glob("*.bsp"), key=lambda p: p.name.lower())


__all__ = ["BudgetLine", "BuildLoad", "MergeComparison", "MergeRow", "ProjectLoad", "budget",
           "map_files", "merge_comparison", "project_load"]
