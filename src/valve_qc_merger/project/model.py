"""A studio project: a folder with ``project.toml``, ``assets/`` and ``builds/``.

::

    MyPack/
      project.toml          name, settings, every asset and build definition
      assets/<kind>/<name>/ one imported model, decompiled (QC + SMD + BMP)
      builds/<name>/        input/ (staged copy), output/, last_run.json

An *asset* is one decompiled model with a kind (``v``, ``p``, ``w``,
``player``, ``zhands``). A *build* runs one merge service over a set of
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
    EXIT_OK,
    Reporter,
    ServiceResult,
    options_from_dict,
)
from valve_qc_merger.services.compile import CompileOptions, run_compile
from valve_qc_merger.services.decompile import DecompileOptions, find_models, run_decompile
from valve_qc_merger.services.merge_player import MergePlayerOptions, run_merge_player
from valve_qc_merger.services.merge_players import MergePlayersOptions, run_merge_players
from valve_qc_merger.services.merge_view import MergeViewOptions, run_merge_view
from valve_qc_merger.services.merge_world import MergeWorldOptions, run_merge_world
from valve_qc_merger.services.merge_zhands import MergeZhandsOptions, run_merge_zhands
from valve_qc_merger.services.retarget import RetargetOptions, run_retarget

FORMAT_VERSION = 1
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

_ZOMBIE_RE = re.compile(r"^v_(?P<zombie>.+?)_(?P<role>knife|grenade)(?:_.+)?$", re.IGNORECASE)


@dataclass
class Asset:
    name: str
    kind: str
    path: str  # relative to the project root, POSIX separators
    source: str | None = None  # where it was imported from
    notes: str = ""


@dataclass
class Build:
    name: str
    kind: str  # a BUILD_KINDS key
    assets: list[str] = field(default_factory=list)  # empty: every matching asset
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
        for entry in data.get("assets", []):
            asset = Asset(**entry)
            project.assets[asset.name] = asset
        for entry in data.get("builds", []):
            build = Build(**entry)
            project._validate_build(build)
            project.builds[build.name] = build
        return project

    def save(self) -> None:
        data: dict[str, Any] = {
            "project": {"name": self.name, "format": FORMAT_VERSION},
            "settings": {"studiomdl": self.settings.studiomdl,
                         "hlam": self.settings.hlam},
            "assets": [vars(a) for a in sorted(self.assets.values(),
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
                          overwrite: bool = False) -> list[Asset]:
        """Copy one decompiled model folder (with a .qc) or every such
        subfolder of ``source`` into ``assets/<kind>/<name>``."""
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
                          source=str(directory.resolve()))
            self.assets[name] = asset
            added.append(asset)
        if kind is None:
            refine_kinds(list(self.assets.values()), self.root)
            for asset in list(self.assets.values()):
                self._move_to_kind_folder(asset)
        self.save()
        return added

    def import_mdl(self, source: Path, *, kind: str | None = None,
                   overwrite: bool = False,
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
            added = self.import_decompiled(staging, kind=kind, overwrite=overwrite)
            originals = {m.stem: m for m in models}
            for asset in added:
                if asset.name in originals:
                    asset.source = str(originals[asset.name].resolve())
            self.save()
            return added
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def remove_asset(self, name: str, *, delete_files: bool = True) -> None:
        asset = self.assets.pop(name)
        if delete_files:
            shutil.rmtree(self.root / asset.path, ignore_errors=True)
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
            chosen = sorted((a for a in self.assets.values() if a.kind in accepted),
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
        if build.retarget:
            retargeted = base / "retarget"
            for done, asset in enumerate(assets):
                reporter.check()
                reporter.progress(done, len(assets), f"retarget {asset.name}")
                opts = options_from_dict(RetargetOptions, {
                    **build.retarget_options,
                    "weapon_dir": str(staged / asset.name),
                    "out": str(retargeted / asset.name),
                })
                outcome = run_retarget(opts, reporter)
                if not outcome.ok:
                    reporter.log(f"  retarget {asset.name}: skipped "
                                 f"(exit {outcome.exit_code})")
                    shutil.rmtree(retargeted / asset.name, ignore_errors=True)
            models_dir = retargeted
            options["shared_hands"] = True
        opts = options_from_dict(kind.options, {
            **options, "models_dir": str(models_dir), "out": str(output),
        })
        result = kind.run(opts, reporter)
        record = {
            "build": name,
            "kind": build.kind,
            "exit_code": result.exit_code,
            "started": started,
            "seconds": round(time.time() - started, 2),
            "outputs": [p.relative_to(self.root).as_posix() for p in result.outputs],
            "failures": result.failures,
            "gates": [vars(g) for g in result.gates],
        }
        (base / "last_run.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
        return result

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


def _build_dict(build: Build) -> dict[str, Any]:
    data: dict[str, Any] = {"name": build.name, "kind": build.kind,
                            "assets": list(build.assets), "retarget": build.retarget}
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
    "Project",
    "ProjectError",
    "Settings",
    "classify",
    "refine_kinds",
]
