"""Whole-project steps: import a server's mod folder, start from a template."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from valve_qc_merger.services.base import Reporter

if TYPE_CHECKING:
    from valve_qc_merger.project.model import Project

# key -> (title, what it sets up)
TEMPLATES = {
    "empty": ("Empty", "No categories or builds — add what you need."),
    "classic": ("Classic weapons", "Categories for every weapon class (pistols, rifles, "
                "SMGs, shotguns, snipers, machine guns, knives, grenades); create the "
                "builds of a category once it has models."),
    "zombie": ("Zombie server", "Categories for weapons and zombie hands, a zombie-hands "
               "build, and weapon builds (view on our hands, player-held, world)."),
}
CLASSIC = ["pistols", "rifles", "smgs", "shotguns", "snipers", "machine guns", "knives",
           "grenades"]


def apply_template(project: Project, key: str) -> None:
    """Set a new project up from template ``key`` (see :data:`TEMPLATES`)."""
    from valve_qc_merger.project.model import Build
    if key == "classic":
        for category in CLASSIC:
            project.add_category(category, save=False)
    elif key == "zombie":
        for category in ("weapons", "zombie hands"):
            project.add_category(category, save=False)
        project.add_build(Build("zombie_hands", "merge-zhands", category="zombie hands",
                                options={"name": "v_zombie_hands"}))
        project.add_build(Build("weapons_view", "merge-v", category="weapons",
                                retarget=True, options={"name": "v_weapons"}))
        project.add_build(Build("weapons_player", "merge-p", category="weapons",
                                options={"name": "p_weapons"}))
        project.add_build(Build("weapons_world", "merge-w", category="weapons",
                                options={"name": "w_weapons"}))
        project.settings.client_sounds = "generic"
    elif key != "empty":
        raise ValueError(f"unknown template {key!r}")
    project.save()


@dataclass
class ServerImport:
    models: list[str] = field(default_factory=list)  # assets added
    skipped: list[str] = field(default_factory=list)  # already in the project
    failed: list[str] = field(default_factory=list)  # "<file>: why"
    sounds: list[str] = field(default_factory=list)
    sprites: list[str] = field(default_factory=list)


def import_server_folder(project: Project, root: Path, *, models: bool = True,
                         sounds: str = "used", sprites: bool = True,
                         category: str | None = None,
                         reporter: Reporter | None = None) -> ServerImport:
    """Bring a server's mod folder (``…/cstrike``) into the project:
    ``models/`` (each model on its own, existing names skipped), the sounds
    of ``sound/`` (``used``: only those the imported models play — a stock
    folder holds thousands; ``all``; ``none``) and the weapon HUDs of
    ``sprites/`` (``weapon_*.txt`` and the sheets they draw from)."""
    from valve_qc_merger.project import sounds as sound_library
    from valve_qc_merger.project import sprites as sprite_library
    from valve_qc_merger.project.model import ProjectError
    from valve_qc_merger.services.decompile import find_models
    from valve_qc_merger.sprite.hud import sheets_of
    reporter = reporter or Reporter()
    root = Path(root)
    result = ServerImport()
    if models and (root / "models").is_dir():
        files = find_models(root / "models")
        for done, mdl in enumerate(files):
            reporter.check()
            reporter.progress(done, len(files), mdl.name)
            if mdl.stem in project.assets:
                result.skipped.append(mdl.stem)
                continue
            try:
                added = project.import_mdl(mdl, reporter=reporter, category=category)
            except (ProjectError, OSError, ValueError) as exc:
                result.failed.append(f"{mdl.name}: {exc}")
                continue
            result.models += [a.name for a in added]
    sound_root = root / "sound"
    if sounds != "none" and sound_root.is_dir():
        if sounds == "all":
            result.sounds = sound_library.import_sounds(project, [sound_root])
        else:
            wanted = {s.lower() for found in sound_library.asset_sounds(project).values()
                      for s in found}
            files = [p for p in sound_root.rglob("*") if p.is_file()
                     and p.relative_to(sound_root).as_posix().lower() in wanted]
            result.sounds = sound_library.import_sounds(project, files)
    sprite_root = root / "sprites"
    if sprites and sprite_root.is_dir():
        picked: list[Path] = []
        for txt in sorted(sprite_root.glob("weapon_*.txt")):
            picked.append(txt)
            for sheet in sheets_of(txt.read_text(encoding="latin-1", errors="replace")):
                path = root / sheet
                if path.is_file() and path not in picked:
                    picked.append(path)
        result.sprites = sprite_library.import_sprites(project, picked)
    project.save()
    return result


__all__ = ["CLASSIC", "ServerImport", "TEMPLATES", "apply_template", "import_server_folder"]
