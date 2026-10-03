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

import json
import re
import shutil
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


DERIVE_MODES: dict[str, DeriveMode] = {
    "hands": DeriveMode("Swap hands (retarget)", RetargetOptions, run_retarget,
                        "weapon_dir", "_hands"),
    "canon": DeriveMode("Canonical bones (own hands)", CanonicalizeOptions,
                        run_canonicalize, "model_dir", "_canon"),
}
# options the project sets itself; never stored with a derived asset
_DERIVE_FIXED = {"weapon_dir", "model_dir", "out", "qc", "category", "compile",
                 "studiomdl"}

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


@dataclass
class Settings:
    studiomdl: str | None = None
    hlam: str | None = None


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


def _qc_dir(directory: Path) -> Path | None:
    return directory if any(directory.glob("*.qc")) else None


def _uses_grenade_texture(directory: Path, prefix: str = "frogbomb") -> bool:
    return any(p.name.lower().startswith(prefix) for p in directory.rglob("*.bmp"))


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
        project.settings = Settings(studiomdl=settings.get("studiomdl"),
                                    hlam=settings.get("hlam"))
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
                         "hlam": self.settings.hlam},
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
        name = (name or f"{source}{spec.suffix}").strip()
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
                "out": str(staging / "output"),
            })
            reporter.log(f"{spec.title}: {source} -> {name}")
            result = spec.run(opts, reporter)
            if not result.ok or not any((staging / "output").glob("*.qc")):
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
            shutil.move(str(staging / "output"), str(target))
            asset = Asset(name=name, kind=kind, path=relative.as_posix(),
                          notes=existing.notes if existing is not None else "",
                          derived={"from": source, "mode": mode, "options": stored},
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

    def remove_asset(self, name: str, *, delete_files: bool = True) -> None:
        asset = self.assets.pop(name)
        if delete_files:
            shutil.rmtree(self.root / asset.path, ignore_errors=True)
            shutil.rmtree(self._history_dir(name), ignore_errors=True)
        for build in self.builds.values():
            if name in build.assets:
                build.assets.remove(name)
        self.save()

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

    def build_dir(self, name: str) -> Path:
        return self.root / "builds" / name

    def run_build(self, name: str, reporter: Reporter | None = None) -> ServiceResult:
        """Stage the build's assets, run its service, record ``last_run.json``."""
        reporter = reporter or Reporter()
        build = self.builds[name]
        kind = BUILD_KINDS[build.kind]
        assets = self.build_assets(build)
        if not assets:
            reporter.log(f"error: build {name!r} has no assets")
            return ServiceResult(exit_code=EXIT_DISCOVERY)
        base = self.build_dir(name)
        staged = base / "input"
        output = base / "output"
        for directory in (staged, output, base / "retarget"):
            shutil.rmtree(directory, ignore_errors=True)
        staged.mkdir(parents=True)
        started = time.time()
        for asset in assets:
            shutil.copytree(self.asset_dir(asset.name), staged / asset.name)
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
            notes += self._retarget_for_build(assets, staged, base / "retarget", build,
                                              reporter)
            models_dir = base / "retarget"
            options["shared_hands"] = True
        opts = options_from_dict(kind.options, {
            **options, "models_dir": str(models_dir), "out": str(output),
        })
        result = kind.run(opts, reporter)
        result.warnings[:0] = notes
        failed_retargets = [n for n in notes if n.startswith("retarget ")]
        if failed_retargets:
            result.failures[:0] = failed_retargets
            if result.exit_code == EXIT_OK:
                result.exit_code = EXIT_FAIL
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

    def _retarget_for_build(self, assets: list[Asset], staged: Path, retargeted: Path,
                            build: Build, reporter: Reporter) -> list[str]:
        """Bring every asset onto our hands for a shared-hands merge-v build:
        assets made by Retarget (swap hands) and models already wearing our
        hands are taken as they are (their tuned grip kept); the rest are
        retargeted with the build's options. Returns notes for the report."""
        from valve_qc_merger.merge_view.handcheck import dir_wears_hands
        from valve_qc_merger.resources import resource_path
        from valve_qc_merger.retarget.config import DEFAULT_SHARED_HANDS_REFERENCE
        reference = resource_path(Path(DEFAULT_SHARED_HANDS_REFERENCE))
        retargeted.mkdir(parents=True, exist_ok=True)
        notes: list[str] = []
        ready = converted = 0
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
            opts = options_from_dict(RetargetOptions, {
                **build.retarget_options,
                "weapon_dir": str(source),
                "out": str(retargeted / asset.name),
            })
            outcome = run_retarget(opts, reporter)
            if outcome.ok:
                converted += 1
                continue
            why = outcome.failures[-1] if outcome.failures else f"exit {outcome.exit_code}"
            notes.append(f"retarget {asset.name} failed ({why}); left out of the merge")
            reporter.log(f"  warn: {notes[-1]}")
            shutil.rmtree(retargeted / asset.name, ignore_errors=True)
        reporter.log(f"  retarget: {converted} converted, {ready} already on our hands, "
                     f"{len(assets) - converted - ready} failed")
        return notes

    def compile_build(self, name: str, reporter: Reporter | None = None) -> ServiceResult:
        """Compile every QC the build's last run emitted with the configured
        studiomdl."""
        reporter = reporter or Reporter()
        if not self.settings.studiomdl:
            reporter.log("error: set the studiomdl path in the project settings")
            return ServiceResult(exit_code=EXIT_DISCOVERY)
        record_path = self.build_dir(name) / "last_run.json"
        if not record_path.exists():
            reporter.log(f"error: build {name!r} has not been run")
            return ServiceResult(exit_code=EXIT_DISCOVERY)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        total = ServiceResult()
        for done, relative in enumerate(record["outputs"]):
            reporter.check()
            reporter.progress(done, len(record["outputs"]), relative)
            one = run_compile(CompileOptions(qc=self.root / relative,
                                             studiomdl=Path(self.settings.studiomdl)),
                              reporter)
            total.outputs.extend(one.outputs)
            total.failures.extend(one.failures)
            if not one.ok:
                total.exit_code = one.exit_code
        if total.exit_code == EXIT_OK:
            reporter.log(f"compiled {len(total.outputs)} model(s)")
        return total


def _asset_dict(asset: Asset) -> dict[str, Any]:
    data = vars(asset).copy()
    if not data["category"]:
        del data["category"]
    return data


def _build_dict(build: Build) -> dict[str, Any]:
    data: dict[str, Any] = {"name": build.name, "kind": build.kind,
                            "assets": list(build.assets), "retarget": build.retarget,
                            "category": build.category}
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
