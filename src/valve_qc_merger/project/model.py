"""A studio project: a folder with ``project.toml``, ``assets/`` and ``builds/``.

::

    MyPack/
      project.toml          name, settings, every asset and build definition
      assets/<kind>/<name>/ one imported model, decompiled (QC + SMD + BMP)
      builds/<name>/        input/ (staged copy), output/, last_run.json

An *asset* is one decompiled model with a kind (``v``, ``p``, ``w``,
``player``, ``zhands``). A *derived* asset is made from another one by a
service (hand swap, or bone canonicalisation) and remembers how, so it can be
re-run after its source or options change. A *build* runs one merge service over a set of
assets with stored options; a ``merge-v`` build can retarget every asset onto
our hands first (then merges with ``shared_hands``). Inputs are staged as
copies because the services sanitise file names in place.
"""

from __future__ import annotations

import filecmp
import glob
import json
import os
import re
import shutil
import stat
import struct
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from valve_qc_merger.project.toml_write import dumps
from valve_qc_merger.services.base import (
    EXIT_DISCOVERY,
    EXIT_FAIL,
    EXIT_OK,
    Reporter,
    ServiceResult,
    options_from_dict,
)
from valve_qc_merger.services.canonicalize import CanonicalizeOptions, run_canonicalize
from valve_qc_merger.services.compile import CompileOptions, run_compile
from valve_qc_merger.services.decompile import DecompileOptions, find_models, run_decompile
from valve_qc_merger.services.merge_player import MergePlayerOptions, run_merge_player
from valve_qc_merger.services.merge_players import MergePlayersOptions, run_merge_players
from valve_qc_merger.services.merge_view import MergeViewOptions, run_merge_view
from valve_qc_merger.services.merge_world import MergeWorldOptions, run_merge_world
from valve_qc_merger.services.merge_zhands import MergeZhandsOptions, run_merge_zhands
from valve_qc_merger.services.retarget import RetargetOptions, run_retarget
from valve_qc_merger.services.zhands_grenade import (
    ZhandsGrenadeOptions,
    grenade_name,
    run_zhands_grenade,
)

FORMAT_VERSION = 2  # 2: asset categories, category builds
PROJECT_FILE = "project.toml"
ASSET_KINDS = ("v", "p", "w", "player", "zhands")


class ProjectError(RuntimeError):
    """Invalid project file, asset or build definition."""


@dataclass(frozen=True)
class BuildKind:
    """A merge service usable as a build: its options type, entry point and
    the asset kinds it accepts."""

    options: type
    run: Callable[..., ServiceResult]
    asset_kinds: frozenset[str]


BUILD_KINDS: dict[str, BuildKind] = {
    "merge-v": BuildKind(MergeViewOptions, run_merge_view, frozenset({"v"})),
    "merge-p": BuildKind(MergePlayerOptions, run_merge_player, frozenset({"p"})),
    "merge-w": BuildKind(MergeWorldOptions, run_merge_world, frozenset({"w"})),
    "merge-players": BuildKind(MergePlayersOptions, run_merge_players,
                               frozenset({"player"})),
    "merge-zhands": BuildKind(MergeZhandsOptions, run_merge_zhands,
                              frozenset({"zhands"})),
}

@dataclass(frozen=True)
class DeriveMode:
    """A service that turns one asset into a new one."""

    title: str
    options: type
    run: Callable[..., ServiceResult]
    input_field: str  # the options field naming the source folder
    suffix: str  # default name: <source><suffix>
    rename: Callable[[str], str] | None = None  # default name from the source name
    kinds: frozenset[str] | None = None  # source kinds it applies to (None: any)

    def default_name(self, source: str) -> str:
        return self.rename(source) if self.rename else f"{source}{self.suffix}"


DERIVE_MODES: dict[str, DeriveMode] = {
    "hands": DeriveMode("Swap hands (retarget)", RetargetOptions, run_retarget,
                        "weapon_dir", "_hands"),
    "canon": DeriveMode("Canonical bones (own hands)", CanonicalizeOptions,
                        run_canonicalize, "model_dir", "_canon"),
    "grenade": DeriveMode("Make grenade (zombie hands)", ZhandsGrenadeOptions,
                          run_zhands_grenade, "knife_dir", "_grenade",
                          rename=grenade_name, kinds=frozenset({"zhands"})),
}
# options the project sets itself; never stored with a derived asset
_DERIVE_FIXED = {"weapon_dir", "model_dir", "knife_dir", "out", "qc", "category",
                 "compile", "studiomdl", "modelname"}

_ZOMBIE_RE = re.compile(r"^v_(?P<zombie>.+?)_(?P<role>knife|grenade)(?:_.+)?$", re.IGNORECASE)


@dataclass
class Asset:
    name: str
    kind: str
    path: str  # relative to the project root, POSIX separators
    source: str | None = None  # where it was imported from
    notes: str = ""
    # derived assets: {"from": source asset, "mode": DERIVE_MODES key,
    # "options": service options that differ from the defaults}
    derived: dict[str, Any] | None = None
    category: str = ""  # "" = uncategorized


@dataclass
class Build:
    name: str
    kind: str  # a BUILD_KINDS key
    assets: list[str] = field(default_factory=list)  # empty: every matching asset
    category: str | None = None  # with no explicit assets: only this category's
    options: dict[str, Any] = field(default_factory=dict)  # service options
    retarget: bool = False  # merge-v only: retarget each asset first
    retarget_options: dict[str, Any] = field(default_factory=dict)
    deploy_dir: str | None = None  # under the game folder; None: by kind


@dataclass
class Settings:
    studiomdl: str | None = None
    hlam: str | None = None
    game_dir: str | None = None  # the mod folder (…/cstrike): Deploy copies there
    deploy_after_compile: bool = False
    # server budget: what the game DLL + plugins precache (the studio cannot
    # see those) and where plugins put the view models' client sounds
    extra_models: int = 0
    extra_sounds: int = 0
    extra_generic: int = 0
    client_sounds: str = "generic"  # generic (ReHLDS 4096) | sound
    # stock models unprecached (Metamod Unprecacher) and what replaces them
    unprecache: list[str] = field(default_factory=list)
    unprecache_replace: str = ""
    count_stock: bool = True  # budget: count the stock CS models ReGameDLL precaches
    # per client sound (path under sound/, lower case): "sound" | "generic"
    # overriding the automatic choice (attack sequences -> sound)
    sound_precache: dict[str, str] = field(default_factory=dict)
    # sound (lower case) -> the sound to play instead (Find similar sounds):
    # applied to the sound events of every model a build stages
    sound_aliases: dict[str, str] = field(default_factory=dict)


# where Deploy puts a build's models, under the game folder
DEPLOY_DIRS = {"merge-players": "models/player"}
DEFAULT_DEPLOY_DIR = "models"
MANIFEST_SUFFIXES = (".ini", ".json", ".toml")
COMPILE_CACHE = "compile_cache.json"  # builds/<name>/: QC -> digest of its last compile
COMPILED_STASH = ".compiled"  # builds/<name>/: the models of the run before
SERVER_TREE = "cstrike"  # builds/<name>/cstrike: the build's files as on the server


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def classify(name: str) -> str:
    """Asset kind from the model name (refined by :func:`refine_kinds`)."""
    lowered = name.lower()
    for prefix, kind in (("v_", "v"), ("p_", "p"), ("w_", "w")):
        if lowered.startswith(prefix):
            return kind
    return "player"


_PLAYER_SEQUENCE = re.compile(r"(?i)^(?:ref_aim|crouch_aim|ref_shoot|crouch_shoot)")


def model_role(path: Path) -> str:
    """What a compiled ``.mdl`` is: ``weapon`` (a ``v_``/``p_``/``w_`` name),
    ``player`` (``models/player/<x>/<x>.mdl``, or a model with the player
    aim sequences) or ``other`` — a map prop, an effect, an NPC: nothing a
    build of this project takes."""
    path = Path(path)
    if classify(path.stem) != "player":
        return "weapon"
    if path.parent.parent.name.lower() == "player" and path.parent.name.lower() == \
            path.stem.lower():
        return "player"
    try:
        data = path.read_bytes()
        count, index = struct.unpack_from("<2i", data, 136 + 7 * 4)
        labels = [data[index + i * 176:index + i * 176 + 32].split(b"\0", 1)[0]
                  .decode("latin-1") for i in range(max(min(count, 2048), 0))]
    except (OSError, struct.error):
        return "other"
    return "player" if any(_PLAYER_SEQUENCE.match(label) for label in labels) else "other"


class _QuietProgress(Reporter):
    """Logs and cancellation of ``outer``; progress stays the caller's."""

    def __init__(self, outer: Reporter) -> None:
        self.outer = outer

    def log(self, message: str) -> None:
        self.outer.log(message)

    def cancelled(self) -> bool:
        return self.outer.cancelled()


@dataclass
class ImportOutcome:
    added: list[Asset] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # already in the project
    ignored: list[str] = field(default_factory=list)  # props / effects / NPCs
    failed: list[str] = field(default_factory=list)  # "<file>: why"


def _qc_dir(directory: Path) -> Path | None:
    return directory if any(directory.glob("*.qc")) else None


def _uses_grenade_texture(directory: Path, prefix: str = "frogbomb") -> bool:
    return any(p.name.lower().startswith(prefix) for p in directory.rglob("*.bmp"))


def remove_tree(path: Path) -> bool:
    """Delete ``path`` for good; True when it is gone. Unlike
    ``rmtree(ignore_errors=True)`` it gets past what stops Windows: read-only
    files and folders (made writable and retried) and paths over 260
    characters (long CSO texture names: the extended-length path prefix)."""
    if not path.exists():
        return True
    target = path
    if os.name == "nt":
        target = Path("\\\\?\\" + str(path.resolve()))

    def retry(func, failed, _exc) -> None:  # noqa: ANN001 - shutil callback
        for writable in (failed, os.path.dirname(failed)):
            try:
                os.chmod(writable, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except OSError:
                pass
        try:
            func(failed)
        except OSError:
            pass  # still in use: reported by the caller

    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=retry)
    else:  # pragma: no cover - 3.11
        shutil.rmtree(target, onerror=retry)
    return not path.exists()


def refine_kinds(assets: list[Asset], root: Path) -> None:
    """CSO zombie hands: ``v_<z>_grenade`` holding the frog grenade is
    ``zhands``, and so is every ``v_<z>_knife[_variant]`` of that zombie."""
    zombies: set[str] = set()
    for asset in assets:
        match = _ZOMBIE_RE.match(asset.name)
        if (asset.kind == "v" and match and match.group("role").lower() == "grenade"
                and _uses_grenade_texture(root / asset.path)):
            zombies.add(match.group("zombie").lower())
    for asset in assets:
        match = _ZOMBIE_RE.match(asset.name)
        if asset.kind == "v" and match and match.group("zombie").lower() in zombies:
            asset.kind = "zhands"


_KIND_PREFIX = re.compile(r"^[vpw]_", re.IGNORECASE)


def weapon_key(name: str) -> str:
    """The weapon an asset belongs to: its name without the ``v_``/``p_``/``w_``
    prefix (``v_deagle``, ``p_deagle``, ``w_deagle`` -> ``deagle``)."""
    return _KIND_PREFIX.sub("", name).lower()


def _valid_category(name: str) -> str:
    name = name.strip()
    if not name or any(c in name for c in '/\\:*?"<>|') or name in {".", ".."}:
        raise ProjectError(f"invalid category name {name!r}")
    return name


# --------------------------------------------------------------------------- #
# Project
# --------------------------------------------------------------------------- #
class Project:
    def __init__(self, root: Path, name: str) -> None:
        self.root = root
        self.name = name
        self.settings = Settings()
        self.assets: dict[str, Asset] = {}
        self.builds: dict[str, Build] = {}
        self.categories: list[str] = []  # kept even while empty

    # -- persistence -------------------------------------------------------
    @classmethod
    def create(cls, root: Path, name: str | None = None) -> Project:
        root = Path(root)
        if (root / PROJECT_FILE).exists():
            raise ProjectError(f"a project already exists in {root}")
        root.mkdir(parents=True, exist_ok=True)
        (root / "assets").mkdir(exist_ok=True)
        (root / "builds").mkdir(exist_ok=True)
        project = cls(root, name or root.name)
        project.save()
        return project

    @classmethod
    def open(cls, root: Path) -> Project:
        root = Path(root)
        path = root / PROJECT_FILE
        if not path.exists():
            raise ProjectError(f"no {PROJECT_FILE} in {root}")
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
        meta = data.get("project", {})
        version = meta.get("format", FORMAT_VERSION)
        if version > FORMAT_VERSION:
            raise ProjectError(f"project format {version} is newer than "
                               f"this program ({FORMAT_VERSION})")
        project = cls(root, meta.get("name", root.name))
        settings = data.get("settings", {})
        project.settings = Settings(
            studiomdl=settings.get("studiomdl"), hlam=settings.get("hlam"),
            game_dir=settings.get("game_dir"),
            deploy_after_compile=bool(settings.get("deploy_after_compile", False)),
            extra_models=int(settings.get("extra_models", 0)),
            extra_sounds=int(settings.get("extra_sounds", 0)),
            extra_generic=int(settings.get("extra_generic", 0)),
            client_sounds=str(settings.get("client_sounds", "generic")),
            # shields live in models/shield/ (written as models/ before 0.9)
            unprecache=[re.sub(r"^models/([vp])_shield_", r"models/shield/\1_shield_",
                               str(p)) for p in settings.get("unprecache", [])],
            unprecache_replace=str(settings.get("unprecache_replace", "")),
            count_stock=bool(settings.get("count_stock", True)),
            sound_precache={str(k): str(v) for k, v in
                            dict(settings.get("sound_precache", {})).items()},
            sound_aliases={str(k): str(v) for k, v in
                           dict(settings.get("sound_aliases", {})).items()})
        project.categories = list(meta.get("categories", []))
        for entry in data.get("assets", []):
            asset = Asset(**entry)
            project.assets[asset.name] = asset
            if asset.category and asset.category not in project.categories:
                project.categories.append(asset.category)
        for entry in data.get("builds", []):
            build = Build(**entry)
            project._validate_build(build)
            project.builds[build.name] = build
        return project

    def save(self) -> None:
        data: dict[str, Any] = {
            "project": {"name": self.name, "format": FORMAT_VERSION,
                        "categories": sorted(self.categories, key=str.lower)},
            "settings": {"studiomdl": self.settings.studiomdl,
                         "hlam": self.settings.hlam,
                         "game_dir": self.settings.game_dir,
                         "deploy_after_compile": self.settings.deploy_after_compile,
                         "extra_models": self.settings.extra_models,
                         "extra_sounds": self.settings.extra_sounds,
                         "extra_generic": self.settings.extra_generic,
                         "client_sounds": self.settings.client_sounds,
                         "unprecache": self.settings.unprecache,
                         "unprecache_replace": self.settings.unprecache_replace,
                         "count_stock": self.settings.count_stock,
                         "sound_precache": dict(sorted(
                             self.settings.sound_precache.items())),
                         "sound_aliases": dict(sorted(
                             self.settings.sound_aliases.items()))},
            "assets": [_asset_dict(a) for a in sorted(self.assets.values(),
                                                      key=lambda a: (a.kind, a.name))],
            "builds": [_build_dict(b) for b in self.builds.values()],
        }
        tmp = self.root / (PROJECT_FILE + ".tmp")
        tmp.write_text(dumps(data), encoding="utf-8")
        tmp.replace(self.root / PROJECT_FILE)

    # -- assets ------------------------------------------------------------
    def asset_dir(self, name: str) -> Path:
        return self.root / self.assets[name].path

    def import_decompiled(self, source: Path, *, kind: str | None = None,
                          overwrite: bool = False,
                          category: str | None = None) -> list[Asset]:
        """Copy one decompiled model folder (with a .qc) or every such
        subfolder of ``source`` into ``assets/<kind>/<name>``. ``category``
        files them there; without it a model joins the category of its
        weapon's other models (``w_deagle`` follows ``v_deagle``)."""
        if category:
            category = self.add_category(category, save=False)
        source = Path(source)
        dirs = ([source] if _qc_dir(source) else
                sorted(d for d in source.iterdir() if d.is_dir() and _qc_dir(d)))
        if not dirs:
            raise ProjectError(f"no decompiled model (.qc) in {source}")
        if kind is not None and kind not in ASSET_KINDS:
            raise ProjectError(f"unknown asset kind {kind!r}")
        added: list[Asset] = []
        for directory in dirs:
            name = directory.name
            if name in self.assets and not overwrite:
                raise ProjectError(f"asset {name!r} already exists")
            if name in self.assets:
                self.remove_asset(name)
            asset_kind = kind or classify(name)
            relative = Path("assets") / asset_kind / name
            shutil.copytree(directory, self.root / relative)
            asset = Asset(name=name, kind=asset_kind, path=relative.as_posix(),
                          source=str(directory.resolve()),
                          category=category or self._sibling_category(name))
            self.assets[name] = asset
            added.append(asset)
        if kind is None:
            refine_kinds(list(self.assets.values()), self.root)
            for asset in list(self.assets.values()):
                self._move_to_kind_folder(asset)
        self.save()
        return added

    def import_models(self, sources: list[Path], *, category: str | None = None,
                      reporter: Reporter | None = None,
                      only_known: bool = True) -> ImportOutcome:
        """Import every ``.mdl`` of ``sources`` (files or folders) one by one:
        names already in the project are skipped, a model that fails is
        reported and the rest go on, and (``only_known``) models found in a
        folder that are neither weapons nor players — map props, effects — are
        left out (a file named on its own is always imported)."""
        reporter = reporter or Reporter()
        outcome = ImportOutcome()
        models: list[Path] = []
        chosen: set[Path] = set()
        for source in sources:
            if Path(source).is_file():
                chosen.add(Path(source))
            for model in find_models(Path(source)):
                if model not in models:
                    models.append(model)
        taken: set[str] = set()
        for done, model in enumerate(models):
            reporter.check()
            reporter.progress(done, len(models), model.name)
            if model.stem in self.assets or model.stem.lower() in taken:
                outcome.skipped.append(model.stem)
                continue
            if only_known and model not in chosen and model_role(model) == "other":
                outcome.ignored.append(model.stem)
                continue
            try:
                added = self.import_mdl(model, category=category,
                                        reporter=_QuietProgress(reporter))
            except (ProjectError, OSError, ValueError) as exc:
                outcome.failed.append(f"{model.name}: {exc}")
                continue
            outcome.added += added
            taken.update(a.name.lower() for a in added)
        return outcome

    def import_mdl(self, source: Path, *, kind: str | None = None,
                   overwrite: bool = False, category: str | None = None,
                   reporter: Reporter | None = None) -> list[Asset]:
        """Decompile one ``.mdl`` (or every model in a folder) in process and
        import the results like :meth:`import_decompiled`."""
        source = Path(source)
        models = find_models(source)
        if not models:
            raise ProjectError(f"no .mdl in {source}")
        clashes = [m.stem for m in models if m.stem in self.assets and not overwrite]
        if clashes:
            raise ProjectError(f"assets already exist: {clashes}")
        staging = self.root / ".import"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            outcome = run_decompile(DecompileOptions(source=source, out=staging),
                                    reporter or Reporter())
            if not outcome.outputs:
                raise ProjectError(f"nothing decompiled from {source}: {outcome.failures}")
            added = self.import_decompiled(staging, kind=kind, overwrite=overwrite,
                                           category=category)
            originals = {m.stem: m for m in models}
            for asset in added:
                if asset.name in originals:
                    asset.source = str(originals[asset.name].resolve())
            self.save()
            return added
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # -- categories ----------------------------------------------------------
    def weapon_of(self, name: str) -> str:
        """:func:`weapon_key` of an asset, through its derivation chain
        (``v_janus1_hands`` belongs to ``janus1``)."""
        seen: set[str] = set()
        while name in self.assets and self.assets[name].derived and name not in seen:
            seen.add(name)
            name = self.assets[name].derived["from"]
        return weapon_key(name)

    def siblings(self, name: str) -> list[str]:
        """The other assets of the same weapon (v_/p_/w_ and derived ones)."""
        key = self.weapon_of(name)
        return sorted(n for n in self.assets if n != name and self.weapon_of(n) == key)

    def _sibling_category(self, name: str) -> str:
        key = weapon_key(name)
        for other in sorted(self.assets):
            if other != name and self.weapon_of(other) == key and self.assets[other].category:
                return self.assets[other].category
        return ""

    def add_category(self, name: str, *, save: bool = True) -> str:
        name = _valid_category(name)
        existing = {c.lower(): c for c in self.categories}
        if name.lower() in existing:
            return existing[name.lower()]
        self.categories.append(name)
        if save:
            self.save()
        return name

    def category_assets(self, category: str) -> list[str]:
        return sorted(n for n, a in self.assets.items() if a.category == category)

    def set_category(self, names: list[str], category: str, *,
                     with_siblings: bool = True) -> list[str]:
        """File ``names`` (and, by default, every other asset of the same
        weapons) under ``category`` ("" = uncategorized). Returns every
        asset that moved."""
        if category:
            category = self.add_category(category, save=False)
        missing = [n for n in names if n not in self.assets]
        if missing:
            raise ProjectError(f"unknown assets {missing}")
        targets = dict.fromkeys(names)
        if with_siblings:
            for name in names:
                targets.update(dict.fromkeys(self.siblings(name)))
        moved = [n for n in targets if self.assets[n].category != category]
        for name in moved:
            self.assets[name].category = category
        self.save()
        return moved

    def rename_category(self, old: str, new: str) -> None:
        if old not in self.categories:
            raise ProjectError(f"no category {old!r}")
        new = _valid_category(new)
        if new != old and new.lower() in {c.lower() for c in self.categories if c != old}:
            raise ProjectError(f"category {new!r} already exists")
        self.categories = [new if c == old else c for c in self.categories]
        for asset in self.assets.values():
            if asset.category == old:
                asset.category = new
        for build in self.builds.values():
            if build.category == old:
                build.category = new
        self.save()

    def remove_category(self, name: str) -> None:
        """Forget a category; its assets become uncategorized and builds that
        selected it fall back to their kind's every asset."""
        if name not in self.categories:
            raise ProjectError(f"no category {name!r}")
        self.categories.remove(name)
        for asset in self.assets.values():
            if asset.category == name:
                asset.category = ""
        for build in self.builds.values():
            if build.category == name:
                build.category = None
        self.save()

    def create_category_builds(self, category: str) -> list[Build]:
        """One build per asset kind present in ``category`` (merge-v with
        "on our hands first"), named ``<category>_<kind>``; existing names
        are left alone."""
        if category not in self.categories:
            raise ProjectError(f"no category {category!r}")
        slug = re.sub(r"[^0-9A-Za-z]+", "_", category).strip("_").lower() or "category"
        kinds = {self.assets[n].kind for n in self.category_assets(category)}
        made: list[Build] = []
        for kind_name, spec in BUILD_KINDS.items():
            if not (spec.asset_kinds & kinds):
                continue
            prefix = next(iter(sorted(spec.asset_kinds)))
            name = f"{slug}_{prefix}"
            if name in self.builds:
                continue
            options = ({"name": f"{prefix}_{slug}"} if prefix in ("v", "p", "w") else {})
            build = Build(name=name, kind=kind_name, category=category, options=options,
                          retarget=kind_name == "merge-v")
            self._validate_build(build)
            self.builds[name] = build
            made.append(build)
        self.save()
        return made

    # -- derived assets ----------------------------------------------------
    def derive_asset(self, source: str, mode: str, options: dict[str, Any] | None = None,
                     *, name: str | None = None,
                     reporter: Reporter | None = None) -> tuple[ServiceResult, Asset | None]:
        """Run ``mode`` (a :data:`DERIVE_MODES` key) over asset ``source`` and
        store the result as a new asset (default name ``<source><suffix>``).
        Re-deriving an existing derived asset replaces it (undo-able)."""
        reporter = reporter or Reporter()
        if source not in self.assets:
            raise ProjectError(f"no asset {source!r}")
        spec = DERIVE_MODES.get(mode)
        if spec is None:
            raise ProjectError(f"unknown derive mode {mode!r}")
        name = (name or spec.default_name(source)).strip()
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise ProjectError(f"invalid asset name {name!r}")
        if name == source:
            raise ProjectError("a derived asset needs a name of its own")
        existing = self.assets.get(name)
        if existing is not None and existing.derived is None:
            raise ProjectError(f"asset {name!r} already exists (imported, not derived)")
        stored = {k: v for k, v in (options or {}).items() if k not in _DERIVE_FIXED}
        options_from_dict(spec.options, {**stored, spec.input_field: ".", "out": "."})

        staging = self.root / ".derive" / name
        shutil.rmtree(staging, ignore_errors=True)
        try:
            # services sanitise their input in place: work on a copy, named
            # like the source (retarget keys grip_tuning.json by folder name)
            staged = staging / "input" / source
            shutil.copytree(self.asset_dir(source), staged)
            opts = options_from_dict(spec.options, {
                **stored, spec.input_field: str(staged),
                "out": str(staging / "output" / name),
            })
            reporter.log(f"{spec.title}: {source} -> {name}")
            result = spec.run(opts, reporter)
            produced = staging / "output" / name  # named like the asset ($modelname)
            if not result.ok or not any(produced.glob("*.qc")):
                if result.ok:
                    result.exit_code = EXIT_FAIL
                return result, None
            kind = self.assets[source].kind
            relative = Path("assets") / kind / name
            if existing is not None:
                self.snapshot_asset(name)
                shutil.rmtree(self.root / existing.path, ignore_errors=True)
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(produced), str(target))
            asset = Asset(name=name, kind=kind, path=relative.as_posix(),
                          notes=existing.notes if existing is not None else "",
                          derived={"from": source, "mode": mode, "options": stored,
                                   "at": round(time.time(), 3)},
                          category=self.assets[source].category)
            self.assets[name] = asset
            self.save()
            return result, asset
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if (self.root / ".derive").is_dir() and not any((self.root / ".derive").iterdir()):
                (self.root / ".derive").rmdir()

    def rederive_asset(self, name: str, options: dict[str, Any] | None = None,
                       reporter: Reporter | None = None) -> tuple[ServiceResult, Asset | None]:
        """Re-run a derived asset from its source (with new options if given)."""
        asset = self.assets.get(name)
        if asset is None or asset.derived is None:
            raise ProjectError(f"{name!r} is not a derived asset")
        derived = asset.derived
        return self.derive_asset(
            derived["from"], derived["mode"],
            derived.get("options", {}) if options is None else options,
            name=name, reporter=reporter)

    # -- edit history (bone tools) -----------------------------------------
    HISTORY_LIMIT = 10

    def _history_dir(self, name: str) -> Path:
        return self.root / ".history" / name

    def snapshot_asset(self, name: str) -> Path:
        """Copy the asset's QC and SMD files aside before an edit (the last
        :data:`HISTORY_LIMIT` are kept); textures never change, so they're not."""
        source = self.asset_dir(name)
        history = self._history_dir(name)
        history.mkdir(parents=True, exist_ok=True)
        existing = sorted(int(p.name) for p in history.iterdir() if p.name.isdigit())
        target = history / str((existing[-1] + 1) if existing else 1)
        for path in source.rglob("*"):
            if path.is_file() and path.suffix.lower() in (".qc", ".smd"):
                dest = target / path.relative_to(source)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
        for old in existing[: max(0, len(existing) + 1 - self.HISTORY_LIMIT)]:
            shutil.rmtree(history / str(old), ignore_errors=True)
        return target

    def can_undo(self, name: str) -> bool:
        history = self._history_dir(name)
        return history.exists() and any(p.name.isdigit() for p in history.iterdir())

    def undo_asset(self, name: str) -> bool:
        """Restore the newest snapshot of the asset; False if there is none."""
        history = self._history_dir(name)
        if not self.can_undo(name):
            return False
        latest = history / str(max(int(p.name) for p in history.iterdir() if p.name.isdigit()))
        target = self.asset_dir(name)
        for path in target.rglob("*"):
            if path.is_file() and path.suffix.lower() in (".qc", ".smd"):
                path.unlink()
        for path in latest.rglob("*"):
            if path.is_file():
                dest = target / path.relative_to(latest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
        shutil.rmtree(latest)
        return True

    def remove_asset(self, name: str, *, delete_files: bool = True) -> list[Path]:
        """Forget an asset (and delete its folder and undo history). Returns
        the folders that could not be deleted (a file open elsewhere)."""
        asset = self.assets.pop(name)
        leftovers: list[Path] = []
        if delete_files:
            leftovers = [folder for folder in (self.root / asset.path,
                                               self._history_dir(name))
                         if not remove_tree(folder)]
        for build in self.builds.values():
            if name in build.assets:
                build.assets.remove(name)
        self.save()
        return leftovers

    def set_kind(self, name: str, kind: str) -> None:
        if kind not in ASSET_KINDS:
            raise ProjectError(f"unknown asset kind {kind!r}")
        asset = self.assets[name]
        asset.kind = kind
        self._move_to_kind_folder(asset)
        self.save()

    def _move_to_kind_folder(self, asset: Asset) -> None:
        wanted = (Path("assets") / asset.kind / asset.name).as_posix()
        if asset.path == wanted:
            return
        target = self.root / wanted
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.root / asset.path), str(target))
        asset.path = wanted

    # -- builds ------------------------------------------------------------
    def _validate_build(self, build: Build) -> None:
        kind = BUILD_KINDS.get(build.kind)
        if kind is None:
            raise ProjectError(f"build {build.name!r}: unknown kind {build.kind!r}")
        if build.retarget and build.kind != "merge-v":
            raise ProjectError(f"build {build.name!r}: retarget is merge-v only")
        if build.category is not None and build.assets:
            raise ProjectError(f"build {build.name!r}: pick assets OR a category, not both")
        # Fail early on unknown option names (placeholders for the paths).
        options_from_dict(kind.options, {**build.options, "models_dir": ".", "out": "."})
        if build.retarget:
            options_from_dict(RetargetOptions,
                              {**build.retarget_options, "weapon_dir": "."})

    def add_build(self, build: Build) -> None:
        if build.name in self.builds:
            raise ProjectError(f"build {build.name!r} already exists")
        self._validate_build(build)
        self.builds[build.name] = build
        self.save()

    def update_build(self, build: Build) -> None:
        """Replace an existing build's definition (validated, then saved)."""
        if build.name not in self.builds:
            raise ProjectError(f"no build {build.name!r}")
        self._validate_build(build)
        self.builds[build.name] = build
        self.save()

    def remove_build(self, name: str) -> None:
        del self.builds[name]
        shutil.rmtree(self.root / "builds" / name, ignore_errors=True)
        self.save()

    def build_assets(self, build: Build) -> list[Asset]:
        """The assets a build merges (see :meth:`superseded_assets`)."""
        chosen = self.chosen_assets(build)
        superseded = self.superseded_assets(build, chosen)
        return [a for a in chosen if a.name not in superseded]

    def chosen_assets(self, build: Build) -> list[Asset]:
        """The assets a build names (or its kind/category selects)."""
        accepted = BUILD_KINDS[build.kind].asset_kinds
        if build.assets:
            missing = [n for n in build.assets if n not in self.assets]
            if missing:
                raise ProjectError(f"build {build.name!r}: unknown assets {missing}")
            chosen = [self.assets[n] for n in build.assets]
        else:
            chosen = sorted((a for a in self.assets.values() if a.kind in accepted
                             and (build.category is None or a.category == build.category)),
                            key=lambda a: a.name)
        wrong = [a.name for a in chosen if a.kind not in accepted]
        if wrong:
            raise ProjectError(f"build {build.name!r} ({build.kind}) cannot take "
                               f"assets of another kind: {wrong}")
        return chosen

    def _origin(self, asset: Asset) -> str:
        """The non-derived asset ``asset`` was (transitively) made from."""
        seen = {asset.name}
        while asset.derived and asset.derived.get("from") in self.assets:
            asset = self.assets[asset.derived["from"]]
            if asset.name in seen:
                break
            seen.add(asset.name)
        return asset.name

    def superseded_assets(self, build: Build, chosen: list[Asset]) -> dict[str, str]:
        """``{dropped: kept}`` for a build that puts every asset on our hands
        (retarget, or merge-v with shared hands):
        one weapon must not be merged twice, so when a weapon's Retarget
        (swap hands) result is in the build, its source and other variants
        are left out — the swap-hands asset carries the tuned grip."""
        if not (build.retarget or (build.kind == "merge-v"
                                   and build.options.get("shared_hands"))):
            return {}
        swapped: dict[str, str] = {}
        for asset in chosen:
            if asset.derived and asset.derived.get("mode") == "hands":
                swapped.setdefault(self._origin(asset), asset.name)
        return {a.name: swapped[self._origin(a)] for a in chosen
                if self._origin(a) in swapped and a.name != swapped[self._origin(a)]}

    def build_dir(self, name: str) -> Path:
        return self.root / "builds" / name

    def _stage(self, name: str, work: Path, reporter: Reporter
               ) -> tuple[list[Asset], Path, dict[str, Any], list[str]] | None:
        """Copy the build's assets to ``work/input`` (retargeting into
        ``work/retarget`` when asked). Returns (assets, models_dir, service
        options, notes) or None when the build has no assets."""
        build = self.builds[name]
        assets = self.build_assets(build)
        for dropped, kept in self.superseded_assets(build, self.chosen_assets(build)).items():
            reporter.log(f"  {dropped}: left out, {kept} is the same weapon on our hands")
        if not assets:
            reporter.log(f"error: build {name!r} has no assets")
            return None
        staged = work / "input"
        for directory in (staged, work / "retarget"):
            shutil.rmtree(directory, ignore_errors=True)
        staged.mkdir(parents=True)
        for asset in assets:
            shutil.copytree(self.asset_dir(asset.name), staged / asset.name)
        if self.settings.sound_aliases:
            self._alias_staged_sounds(staged, reporter)
        reporter.log(f"build {name}: {build.kind} over {len(assets)} asset(s)")
        options = dict(build.options)
        models_dir = staged
        notes: list[str] = []
        names = {a.name for a in assets}
        for asset in assets:
            origin = asset.derived["from"] if asset.derived else None
            if origin in names:
                notes.append(f"{asset.name} and its source {origin} are both in the build: "
                             "the same weapon twice")
        for note in notes:
            reporter.log(f"  warn: {note}")
        if build.retarget:
            notes += self._retarget_for_build(assets, staged, work / "retarget", build,
                                              reporter)
            models_dir = work / "retarget"
            options["shared_hands"] = True
        return assets, models_dir, options, notes

    def _alias_staged_sounds(self, staged: Path, reporter: Reporter) -> None:
        """Point the staged models' sound events at the sounds they share
        (Find similar sounds); the assets themselves stay as imported."""
        from valve_qc_merger.project.sounds import apply_aliases
        changed = 0
        for qc in staged.glob("*/*.qc"):
            text = qc.read_text(encoding="latin-1")
            new = apply_aliases(text, self.settings.sound_aliases)
            if new != text:
                qc.write_text(new, encoding="latin-1")
                changed += 1
        if changed:
            reporter.log(f"  shared sounds: {len(self.settings.sound_aliases)} alias(es) "
                         f"applied to {changed} model(s)")

    @staticmethod
    def _fold_notes(result: ServiceResult, notes: list[str]) -> None:
        result.warnings[:0] = notes
        failed_retargets = [n for n in notes if n.startswith("retarget ")]
        if failed_retargets:
            result.failures[:0] = failed_retargets
            if result.exit_code == EXIT_OK:
                result.exit_code = EXIT_FAIL

    PLANNABLE = frozenset({"merge-v"})

    def plan_build(self, name: str, reporter: Reporter | None = None) -> ServiceResult:
        """Everything a run decides — parts, who goes where, pev_body, who is
        rejected and why — without writing the merge (merge-v only). Staged
        under ``builds/<name>/plan/`` so the last run's output is untouched;
        recorded in ``plan.json``."""
        reporter = reporter or Reporter()
        build = self.builds[name]
        if build.kind not in self.PLANNABLE:
            reporter.log(f"error: {build.kind} builds cannot be planned")
            return ServiceResult(exit_code=EXIT_DISCOVERY)
        work = self.build_dir(name) / "plan"
        started = time.time()
        try:
            staged = self._stage(name, work, reporter)
            if staged is None:
                return ServiceResult(exit_code=EXIT_DISCOVERY)
            _assets, models_dir, options, notes = staged
            opts = options_from_dict(BUILD_KINDS[build.kind].options, {
                **options, "models_dir": str(models_dir), "out": str(work / "out"),
                "plan_only": True,
            })
            result = BUILD_KINDS[build.kind].run(opts, reporter)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self._fold_notes(result, notes)
        record = {"build": name, "exit_code": result.exit_code, "started": started,
                  "seconds": round(time.time() - started, 2),
                  "parts": result.data.get("plan", []), "failures": result.failures,
                  "warnings": notes}
        (self.build_dir(name) / "plan.json").write_text(json.dumps(record, indent=1),
                                                        encoding="utf-8")
        return result

    def run_build(self, name: str, reporter: Reporter | None = None) -> ServiceResult:
        """Stage the build's assets, run its service, record ``last_run.json``."""
        reporter = reporter or Reporter()
        build = self.builds[name]
        kind = BUILD_KINDS[build.kind]
        base = self.build_dir(name)
        output = base / "output"
        self._stash_compiled(name)
        shutil.rmtree(output, ignore_errors=True)
        shutil.rmtree(self.server_tree(name), ignore_errors=True)  # stale until compiled
        started = time.time()
        staged = self._stage(name, base, reporter)
        if staged is None:
            return ServiceResult(exit_code=EXIT_DISCOVERY)
        _assets, models_dir, options, notes = staged
        opts = options_from_dict(kind.options, {
            **options, "models_dir": str(models_dir), "out": str(output),
        })
        result = kind.run(opts, reporter)
        self._fold_notes(result, notes)
        record = {
            "build": name,
            "kind": build.kind,
            "exit_code": result.exit_code,
            "started": started,
            "seconds": round(time.time() - started, 2),
            "outputs": [p.relative_to(self.root).as_posix() for p in result.outputs],
            "failures": result.failures,
            "warnings": notes,
            "gates": [vars(g) for g in result.gates],
        }
        (base / "last_run.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
        return result

    def hands_asset(self, source: str) -> Asset | None:
        """The Retarget (swap hands) asset made from ``source``, if any."""
        for asset in self.assets.values():
            if asset.derived and asset.derived.get("mode") == "hands" \
                    and asset.derived.get("from") == source:
                return asset
        return None

    def _retarget_for_build(self, assets: list[Asset], staged: Path, retargeted: Path,
                            build: Build, reporter: Reporter) -> list[str]:
        """Bring every asset onto our hands for a shared-hands merge-v build.
        Assets made by Retarget (swap hands) and models already wearing our
        hands are taken as they are; for the rest the build reuses their
        swap-hands asset (``<name>_hands``) when it was made with the build's
        options, else it makes (or remakes) one — saved in the project like a
        Retarget from the Explorer, so the next run does not redo it (and
        takes it in place of its source, as for any swap-hands asset).
        Returns notes for the report."""
        from valve_qc_merger.merge_view.handcheck import dir_wears_hands
        from valve_qc_merger.resources import resource_path
        from valve_qc_merger.retarget.config import DEFAULT_SHARED_HANDS_REFERENCE
        reference = resource_path(Path(DEFAULT_SHARED_HANDS_REFERENCE))
        retargeted.mkdir(parents=True, exist_ok=True)
        wanted = {k: v for k, v in build.retarget_options.items() if k not in _DERIVE_FIXED}
        notes: list[str] = []
        ready = converted = reused = 0

        for done, asset in enumerate(assets):
            reporter.check()
            reporter.progress(done, len(assets), f"retarget {asset.name}")
            source = staged / asset.name
            if ((asset.derived and asset.derived.get("mode") == "hands")
                    or dir_wears_hands(source, reference)):
                shutil.copytree(source, retargeted / asset.name)
                reporter.log(f"  retarget {asset.name}: already on our hands, taken as is")
                ready += 1
                continue
            existing = self.hands_asset(asset.name)
            if existing is not None and existing.derived.get("options", {}) == wanted:
                shutil.copytree(self.asset_dir(existing.name), retargeted / existing.name)
                reporter.log(f"  retarget {asset.name}: {existing.name} (made before with "
                             "these settings) reused")
                reused += 1
                continue
            try:
                result, made = self.derive_asset(
                    asset.name, "hands", wanted,
                    name=existing.name if existing is not None else None, reporter=reporter)
            except ProjectError as exc:  # e.g. <name>_hands is an imported asset
                result, made = ServiceResult(exit_code=EXIT_FAIL, failures=[str(exc)]), None
            if made is not None:
                shutil.copytree(self.asset_dir(made.name), retargeted / made.name)
                reporter.log(f"  retarget {asset.name}: saved as {made.name}")
                converted += 1
                continue
            why = result.failures[-1] if result.failures else f"exit {result.exit_code}"
            if build.options.get("standalone_rejects", True):
                # the merge ships it on its own, with the hands it came with
                shutil.copytree(source, retargeted / asset.name)
                notes.append(f"{asset.name}: retarget failed ({why}); shipped on its own "
                             "with its own hands")
            else:
                notes.append(f"retarget {asset.name} failed ({why}); left out of the merge")
            reporter.log(f"  warn: {notes[-1]}")
        reporter.log(f"  retarget: {converted} converted, {reused} reused, {ready} already on "
                     f"our hands, {len(assets) - converted - reused - ready} failed")
        return notes

    # -- deploy ---------------------------------------------------------------
    def deploy_files(self, name: str, root: Path | None = None) -> list[tuple[Path, Path]]:
        """``(source, destination)`` of everything Deploy copies for a build:
        each compiled model (+ its ``T.mdl`` texture file and ``NN.mdl``
        sequence groups) and the manifest,
        renamed ``<output name>_models.<ext>`` so builds don't overwrite each
        other's. Player models go to ``<dir>/<model>/<model>.mdl``."""
        from valve_qc_merger.services.compile import compiled_model_path
        if root is None and not self.settings.game_dir:
            raise ProjectError("set the game folder in the project settings")
        build = self.builds[name]
        record_path = self.build_dir(name) / "last_run.json"
        if not record_path.exists():
            raise ProjectError(f"build {name!r} has not been run")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        target = Path(root if root is not None else self.settings.game_dir) / (
            build.deploy_dir or DEPLOY_DIRS.get(build.kind, DEFAULT_DEPLOY_DIR))
        players = build.kind == "merge-players"
        pairs: list[tuple[Path, Path]] = []
        missing: list[str] = []
        for relative in record.get("outputs", []):
            mdl = compiled_model_path(self.root / relative)
            if not mdl.exists():
                missing.append(mdl.name)
                continue
            groups = sorted(mdl.parent.glob(f"{glob.escape(mdl.stem)}[0-9][0-9].mdl"))
            for path in (mdl, mdl.with_name(f"{mdl.stem}T.mdl"), *groups):
                if path.exists():
                    folder = target / mdl.stem if players else target
                    pairs.append((path, folder / path.name))
        if missing:
            raise ProjectError(f"not compiled yet: {', '.join(missing)} (Build ▸ Compile)")
        output = self.build_dir(name) / "output"
        base = options_from_dict(BUILD_KINDS[build.kind].options, {
            **build.options, "models_dir": ".", "out": "."}).name
        for path in sorted(output.glob("*")):
            if (path.is_file() and path.suffix.lower() in MANIFEST_SUFFIXES
                    and path.name != "inventory.json"):
                stem = path.stem if path.stem.startswith(base) else f"{base}_{path.stem}"
                pairs.append((path, target / f"{stem}{path.suffix}"))
        return pairs

    def build_sound_files(self, name: str, root: Path) -> tuple[list[tuple[Path, Path]],
                                                                 list[str]]:
        """``(source, root/sound/<path>)`` of every sound the build's models
        play — as their QC names it (merge-v's sound folder, shared sounds
        already swapped in) — and the sounds not found. Stock sounds are left
        out: the game has them."""
        from valve_qc_merger.project import sounds as library
        from valve_qc_merger.server.budget import _CLIENT_SOUND
        texts = [qc.read_text(encoding="latin-1")
                 for qc in sorted((self.build_dir(name) / "output").rglob("*.qc"))]
        named = {library.sound_key(s): s.replace("\\", "/").strip()
                 for text in texts for s in _CLIENT_SOUND.findall(text)}
        pairs: list[tuple[Path, Path]] = []
        missing: list[str] = []
        for key in sorted(library.project_sound_kinds(self, texts)):
            sound = named.get(key, key)
            source = library.resolve(self, sound)
            if source is None:
                missing.append(sound)
            else:
                pairs.append((source, Path(root) / "sound" / sound))
        return pairs, missing

    def server_tree(self, name: str) -> Path:
        """``builds/<name>/cstrike``: what the build puts on a server, laid
        out as in the game folder (made by Compile, copied by Deploy)."""
        return self.build_dir(name) / SERVER_TREE

    def stage_server_tree(self, name: str, reporter: Reporter | None = None) -> Path:
        """(Re)make :meth:`server_tree`: the compiled models (+ ``T.mdl`` and
        sequence groups) under the build's deploy folder, player models in
        ``models/player/<model>/<model>.mdl``, the manifest, and every sound
        the models play under ``sound/``. Raises ProjectError when the build
        is not run or compiled."""
        reporter = reporter or Reporter()
        tree = self.server_tree(name)
        pairs = self.deploy_files(name, root=tree)
        sounds, missing = self.build_sound_files(name, tree)
        shutil.rmtree(tree, ignore_errors=True)
        for source, destination in pairs + sounds:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        for sound in missing:
            reporter.log(f"  warn: sound {sound} not found (import it in Sounds)")
        reporter.log(f"server files: {len(pairs)} model/manifest file(s), {len(sounds)} "
                     f"sound(s) in {tree}")
        return tree

    def deploy_pairs(self, name: str) -> list[tuple[Path, Path]]:
        """``(source, destination)`` of Deploy: every file of the build's
        :meth:`server_tree` (made first when missing) into the game folder."""
        if not self.settings.game_dir:
            raise ProjectError("set the game folder in the project settings")
        tree = self.server_tree(name)
        if not tree.is_dir():
            self.stage_server_tree(name)
        game = Path(self.settings.game_dir)
        return [(path, game / path.relative_to(tree))
                for path in sorted(tree.rglob("*")) if path.is_file()]

    def deploy_build(self, name: str, reporter: Reporter | None = None) -> ServiceResult:
        """Copy the build's server files (``builds/<name>/cstrike``) into the
        game folder."""
        reporter = reporter or Reporter()
        result = ServiceResult()
        try:
            pairs = self.deploy_pairs(name)
        except ProjectError as exc:
            reporter.log(f"error: {exc}")
            result.exit_code = EXIT_DISCOVERY
            return result
        for done, (source, destination) in enumerate(pairs):
            reporter.check()
            reporter.progress(done, len(pairs), destination.name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and destination.samefile(source):
                continue
            if destination.exists() and filecmp.cmp(source, destination, shallow=False):
                result.outputs.append(destination)
                continue  # the game has this very file already (a shared sound)
            shutil.copy2(source, destination)
            reporter.log(f"  deployed {destination}")
            result.outputs.append(destination)
        reporter.log(f"deployed {len(pairs)} file(s) to {self.settings.game_dir}")
        return result

    def _stash_compiled(self, name: str) -> None:
        """Before a run wipes ``output/``: keep its compiled models (``.mdl``,
        ``T.mdl``, sequence groups) in ``builds/<name>/.compiled`` so Compile
        can take back the ones whose sources come out the same."""
        output = self.build_dir(name) / "output"
        stash = self.build_dir(name) / COMPILED_STASH
        shutil.rmtree(stash, ignore_errors=True)
        if not output.is_dir():
            return
        for mdl in output.rglob("*.mdl"):
            target = stash / mdl.relative_to(output)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(mdl, target)

    def _compiled_files(self, mdl: Path) -> list[Path]:
        """``mdl`` and its ``T.mdl`` / ``NN.mdl`` companions that exist."""
        groups = sorted(mdl.parent.glob(f"{glob.escape(mdl.stem)}[0-9][0-9].mdl"))
        return [p for p in (mdl, mdl.with_name(f"{mdl.stem}T.mdl"), *groups) if p.exists()]

    def _restore_compiled(self, name: str, mdl: Path) -> bool:
        """Put the stashed compile of ``mdl`` back (see :meth:`_stash_compiled`)."""
        output = self.build_dir(name) / "output"
        stash = self.build_dir(name) / COMPILED_STASH
        try:
            kept = stash / mdl.relative_to(output)
        except ValueError:
            return False
        files = self._compiled_files(kept)
        if not files or files[0] != kept:
            return False
        for path in files:
            shutil.move(path, mdl.parent / path.name)
        return True

    def compile_build(self, name: str, reporter: Reporter | None = None, *,
                      force: bool = False) -> ServiceResult:
        """Compile every QC the build's last run emitted with the configured
        studiomdl. A model whose sources (its QC folder's .qc/.qci/.smd/.bmp
        and the studiomdl itself) are byte for byte those of its last
        successful compile is not compiled again — also across a Run, which
        only stashes the old models. ``force``: compile every one."""
        from valve_qc_merger.services.compile import compiled_model_path, normalize_sources
        reporter = reporter or Reporter()
        if not self.settings.studiomdl:
            reporter.log("error: set the studiomdl path in the project settings")
            return ServiceResult(exit_code=EXIT_DISCOVERY)
        record_path = self.build_dir(name) / "last_run.json"
        if not record_path.exists():
            reporter.log(f"error: build {name!r} has not been run")
            return ServiceResult(exit_code=EXIT_DISCOVERY)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        cache_path = self.build_dir(name) / COMPILE_CACHE
        try:
            cache = {} if force else json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        studiomdl = Path(self.settings.studiomdl)
        total = ServiceResult()
        reused = 0
        try:
            for done, relative in enumerate(record["outputs"]):
                reporter.check()
                reporter.progress(done, len(record["outputs"]), relative)
                qc = self.root / relative
                digest = None
                if qc.is_file():
                    normalize_sources(qc)  # what studiomdl reads, so hash after it
                    digest = _sources_digest(qc, studiomdl)
                    mdl = compiled_model_path(qc)
                    if cache.get(relative) == digest and (
                            mdl.exists() or self._restore_compiled(name, mdl)):
                        reporter.log(f"  up to date: {mdl.name}")
                        total.outputs.append(mdl)
                        reused += 1
                        continue
                cache.pop(relative, None)
                one = run_compile(CompileOptions(qc=qc, studiomdl=studiomdl), reporter)
                total.outputs.extend(one.outputs)
                total.failures.extend(one.failures)
                if one.ok and digest is not None:
                    cache[relative] = digest
                if not one.ok:
                    total.exit_code = one.exit_code
        finally:
            cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
        if total.exit_code == EXIT_OK:
            reporter.log(f"compiled {len(total.outputs) - reused} model(s)"
                         + (f", {reused} up to date" if reused else ""))
            try:
                self.stage_server_tree(name, reporter)
            except ProjectError as exc:
                reporter.log(f"warn: server files not made: {exc}")
            if self.settings.deploy_after_compile and self.settings.game_dir:
                deployed = self.deploy_build(name, reporter)
                if not deployed.ok:
                    total.exit_code = deployed.exit_code
        else:
            reporter.log(f"server files ({SERVER_TREE}/) not made: "
                         f"{len(total.failures)} model(s) failed to compile")
        return total


_COMPILE_INPUTS = {".qc", ".qci", ".smd", ".bmp"}


def _sources_digest(qc: Path, studiomdl: Path) -> str:
    """Digest of what compiling ``qc`` reads: every .qc/.qci/.smd/.bmp under
    its folder (names and bytes) and which studiomdl (path, size, time)."""
    import hashlib
    digest = hashlib.blake2b(digest_size=16)
    try:
        stat_ = studiomdl.stat()
        digest.update(f"{studiomdl.resolve()}|{stat_.st_size}|{stat_.st_mtime_ns}".encode())
    except OSError:
        digest.update(str(studiomdl).encode())
    folder = qc.parent
    for path in sorted(p for p in folder.rglob("*")
                       if p.is_file() and p.suffix.lower() in _COMPILE_INPUTS):
        digest.update(path.relative_to(folder).as_posix().encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _asset_dict(asset: Asset) -> dict[str, Any]:
    data = vars(asset).copy()
    if not data["category"]:
        del data["category"]
    return data


def _build_dict(build: Build) -> dict[str, Any]:
    data: dict[str, Any] = {"name": build.name, "kind": build.kind,
                            "assets": list(build.assets), "retarget": build.retarget,
                            "category": build.category,
                            "deploy_dir": build.deploy_dir}
    if build.options:
        data["options"] = dict(build.options)
    if build.retarget_options:
        data["retarget_options"] = dict(build.retarget_options)
    return data


__all__ = [
    "ASSET_KINDS",
    "Asset",
    "BUILD_KINDS",
    "Build",
    "DERIVE_MODES",
    "Project",
    "ProjectError",
    "Settings",
    "classify",
    "refine_kinds",
    "weapon_key",
]
