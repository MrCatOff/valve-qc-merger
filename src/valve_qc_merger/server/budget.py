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
           stock_models: int = 0) -> dict[str, BudgetLine]:
    """Slots per kind (models / sounds / generic) for one map ("no map": the
    project and the extras alone). ``client_sounds_as``: where the plugin
    precaches the view models' client sounds — ``generic`` (ReHLDS: 4096
    slots, nothing else uses them) or ``sound``. ``stock_models``: the
    stock CS models the game DLL precaches (weapons, shields, players,
    shells — :data:`.stock.STOCK_MODELS`), counted for the user."""
    extra = extra or {}
    parts: dict[str, list[tuple[str, int]]] = {"models": [], "sounds": [], "generic": []}
    if map_resources is not None:
        parts["models"] += [("map brush models (world included)", map_resources.brush_models),
                            ("map entity models / sprites", len(map_resources.models))]
        parts["sounds"].append(("map entity sounds", len(map_resources.sounds)))
    parts["models"].append(("project builds", len(load.models)))
    parts["models"].append(("project sprites", len(load.sprites)))
    parts["generic"].append(("weapon HUD files", len(load.hud_files)))
    target = "sounds" if client_sounds_as == "sound" else "generic"
    parts[target].append(("view-model client sounds", len(load.client_sounds)))
    if stock_models:
        parts["models"].append(("stock CS weapons, shields, players, shells",
                                int(stock_models)))
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


__all__ = ["BudgetLine", "BuildLoad", "ProjectLoad", "budget", "map_files",
           "project_load"]
