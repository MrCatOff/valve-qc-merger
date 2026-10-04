"""The project's sound library: ``<project>/sounds/`` mirrors the game's ``sound/``.

A sound is named by its path under ``sound/`` (``weapons/ak47_clipin.wav``)
— the name model events (5004) and plugins (precache_sound / generic) use.
Fixing a sound keeps the original in ``.history/sounds/`` until undone.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from valve_qc_merger.sound.wav import FixOptions, fix, read_wav

if TYPE_CHECKING:
    from valve_qc_merger.project.model import Project

# a sound event inside a QC $sequence: { event 5004 12 "weapons/x.wav" }
EVENT_SOUND = re.compile(r"\bevent\s+(5004|1004|1008)\s+(-?\d+)\s+\"([^\"]+\.wav)\"",
                         re.IGNORECASE)
_SEQUENCE = re.compile(r'\$sequence\s+"?([^"\s{]+)"?(.*?)(?=\$sequence|\Z)',
                       re.IGNORECASE | re.DOTALL)


def _wavs(base: Path) -> list[Path]:
    """Every WAV under ``base`` — ``.wav`` / ``.WAV`` alike (glob patterns are
    case-sensitive)."""
    return [p for p in base.rglob("*") if p.is_file() and p.suffix.lower() == ".wav"]


def sounds_dir(project: Project) -> Path:
    return project.root / "sounds"


def _history(project: Project, name: str) -> Path:
    return project.root / ".history" / "sounds" / name


def list_sounds(project: Project) -> list[str]:
    base = sounds_dir(project)
    if not base.is_dir():
        return []
    return sorted((p.relative_to(base).as_posix() for p in _wavs(base)), key=str.lower)


def sound_path(project: Project, name: str) -> Path:
    return sounds_dir(project) / name


def _name_for(path: Path, folder: Path | None) -> str:
    """The game name of an imported file: the path after a ``sound``
    folder when there is one, else relative to the imported folder's parent
    (so importing ``…/weapons`` gives ``weapons/x.wav``)."""
    parts = [p.lower() for p in path.parts]
    if "sound" in parts:
        cut = len(parts) - 1 - parts[::-1].index("sound")
        return Path(*path.parts[cut + 1:]).as_posix()
    if folder is not None:
        base = folder if folder.name.lower() == "sound" else folder.parent
        return path.relative_to(base).as_posix()
    return path.name


def import_sounds(project: Project, sources: list[Path]) -> list[str]:
    """Copy WAV files (or every WAV under folders) into the library; returns
    the names imported (existing ones are replaced)."""
    names = []
    for source in sources:
        source = Path(source)
        if source.is_dir():
            files = [(p, source) for p in sorted(source.rglob("*")) if
                     p.is_file() and p.suffix.lower() == ".wav"]
        else:
            files = [(source, None)] if source.suffix.lower() == ".wav" else []
        for path, folder in files:
            name = _name_for(path.resolve(), folder.resolve() if folder else None)
            target = sound_path(project, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not (target.exists() and target.resolve() == path.resolve()):
                shutil.copy2(path, target)  # (re-importing a library file: nothing to copy)
            names.append(name)
    return names


def remove_sound(project: Project, name: str) -> None:
    sound_path(project, name).unlink(missing_ok=True)
    _history(project, name).unlink(missing_ok=True)


def fix_sound(project: Project, name: str, options: FixOptions | None = None) -> None:
    """Rewrite ``name`` engine-safe; the first fix keeps the original for undo."""
    path = sound_path(project, name)
    fixed = fix(read_wav(path), options)
    backup = _history(project, name)
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
    path.write_bytes(fixed)


def can_undo_fix(project: Project, name: str) -> bool:
    return _history(project, name).exists()


def undo_fix(project: Project, name: str) -> None:
    backup = _history(project, name)
    if backup.exists():
        shutil.copy2(backup, sound_path(project, name))
        backup.unlink()


def sequence_sounds(qc_text: str) -> dict[str, list[tuple[int, str]]]:
    """``{sequence name: [(frame, sound)]}`` of the sound events in a QC."""
    out: dict[str, list[tuple[int, str]]] = {}
    for name, body in _SEQUENCE.findall(qc_text):
        events = [(int(frame), sound.replace("\\", "/"))
                  for _event, frame, sound in EVENT_SOUND.findall(body)]
        if events:
            out[name] = sorted(events)
    return out


def asset_sounds(project: Project) -> dict[str, set[str]]:
    """``{asset: the sounds its sequences play}``."""
    from valve_qc_merger.project.qc_edit import qc_file
    out: dict[str, set[str]] = {}
    for name in project.assets:
        try:
            text = qc_file(project.asset_dir(name)).read_text(encoding="latin-1")
        except (OSError, ValueError):
            continue
        sounds = {s for events in sequence_sounds(text).values() for _f, s in events}
        if sounds:
            out[name] = sounds
    return out


def sound_users(project: Project) -> dict[str, list[str]]:
    """``{sound (lower case): [assets playing it]}``."""
    users: dict[str, list[str]] = {}
    for asset, sounds in asset_sounds(project).items():
        for sound in sounds:
            users.setdefault(sound.lower(), []).append(asset)
    return {k: sorted(v) for k, v in users.items()}


def resolve(project: Project, name: str) -> Path | None:
    """Where a sound named by a model is: the library first, then the game
    folder, its ``_downloads`` folder and the base game (case-insensitive)."""
    candidates = [sounds_dir(project)]
    if project.settings.game_dir:
        game = Path(project.settings.game_dir)
        candidates += [game / "sound", game.with_name(game.name + "_downloads") / "sound",
                       game.with_name("valve") / "sound"]
    for base in candidates:
        direct = base / name
        if direct.is_file():
            return direct
        if base.is_dir():
            lowered = name.lower()
            hit = next((p for p in _wavs(base)
                        if p.relative_to(base).as_posix().lower() == lowered), None)
            if hit is not None:
                return hit
    return None


__all__ = ["EVENT_SOUND", "asset_sounds", "can_undo_fix", "fix_sound", "import_sounds",
           "list_sounds", "remove_sound", "resolve", "sequence_sounds", "sound_path",
           "sound_users", "sounds_dir", "undo_fix"]
