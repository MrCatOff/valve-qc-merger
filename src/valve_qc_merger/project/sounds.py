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


# a view model's sound in one of these sequences is a shot or a swing: the
# weapon plugin usually plays it from the server too (emit_sound), so other
# players hear it — that needs precache_sound, not precache_generic
ATTACK_SEQUENCE = re.compile(r"(?i)shoot|fire|attack|slash|stab|shot|swing|burst")
PRECACHE_CHOICES = ("auto", "sound", "generic")
_CLIENT_EVENT = re.compile(r'\bevent\s+5004\s+-?\d+\s+"([^"]+)"', re.IGNORECASE)


def sound_key(path: str) -> str:
    return path.replace("\\", "/").strip().lower()


def client_sound_events(qc_text: str) -> list[tuple[str, str]]:
    """``(sequence, sound)`` of every client sound event (5004) of a QC."""
    out = []
    for name, body in _SEQUENCE.findall(qc_text):
        out += [(name, sound_key(sound)) for sound in _CLIENT_EVENT.findall(body)]
    return out


def resolve_alias(aliases: dict[str, str], sound: str) -> str:
    """The sound ``sound`` is played as (following a chain of aliases)."""
    key, seen = sound_key(sound), set()
    while key in aliases and key not in seen:
        seen.add(key)
        key = sound_key(aliases[key])
    return key


_EVENT_PATH = re.compile(r'(\bevent\s+(?:5004|1004|1008)\s+-?\d+\s+")([^"]+)(")',
                         re.IGNORECASE)


def apply_aliases(qc_text: str, aliases: dict[str, str]) -> str:
    """``qc_text`` with every sound event path replaced by its alias."""
    if not aliases:
        return qc_text
    keys = {sound_key(k): v for k, v in aliases.items()}

    def swap(match: re.Match[str]) -> str:
        key = sound_key(match.group(2))
        if key not in keys:
            return match.group(0)
        target = resolve_alias(keys, key)
        return f"{match.group(1)}{target}{match.group(3)}"
    return _EVENT_PATH.sub(swap, qc_text)


def stock_sounds() -> set[str]:
    """Sounds the game DLL precaches on every map (ReGameDLL, measured)."""
    from valve_qc_merger.server.stock import PRECACHED
    return {sound_key(s) for s in PRECACHED["sounds"]}


def project_sound_kinds(project: Project, qc_texts: list[str]) -> dict[str, str]:
    """:func:`precache_kinds` with the project's mode, choices and aliases;
    sounds the game precaches anyway (stock) left out — they cost nothing."""
    settings = project.settings
    kinds = precache_kinds(qc_texts, settings.client_sounds, settings.sound_precache,
                           aliases=settings.sound_aliases)
    stock = stock_sounds()
    return {k: v for k, v in kinds.items() if k not in stock}


def precache_kinds(qc_texts: list[str], default: str = "generic",
                   overrides: dict[str, str] | None = None,
                   aliases: dict[str, str] | None = None) -> dict[str, str]:
    """``{sound: "sound" | "generic"}`` for every client sound of ``qc_texts``.
    ``default`` "sound": every one through precache_sound. "generic"
    (ReHLDS): only what the shooter hears — sounds of attack sequences
    (shots, swings: the plugin plays them for the others too) still go
    through precache_sound. ``overrides`` (per sound) win."""
    overrides = {sound_key(k): v for k, v in (overrides or {}).items() if v != "auto"}
    aliases = {sound_key(k): v for k, v in (aliases or {}).items()}
    attack: dict[str, bool] = {}
    for text in qc_texts:
        for sequence, sound in client_sound_events(text):
            sound = resolve_alias(aliases, sound)
            attack[sound] = attack.get(sound, False) or bool(ATTACK_SEQUENCE.search(sequence))
    out = {}
    for sound, in_attack in attack.items():
        kind = "sound" if default == "sound" or in_attack else "generic"
        out[sound] = overrides.get(sound, kind)
    return out


def auto_precache(project: Project, name: str) -> str | None:
    """How ``name`` would be precached without a choice of its own ("sound"
    or "generic"), or None when no model plays it as a client sound."""
    from valve_qc_merger.project.qc_edit import qc_file
    texts = []
    for asset in project.assets:
        try:
            texts.append(qc_file(project.asset_dir(asset)).read_text(encoding="latin-1"))
        except (OSError, ValueError):
            continue
    return precache_kinds(texts, project.settings.client_sounds).get(sound_key(name))


def set_precache(project: Project, name: str, choice: str) -> None:
    """Remember ``choice`` ("auto" | "sound" | "generic") for sound ``name``."""
    if choice not in PRECACHE_CHOICES:
        raise ValueError(f"unknown precache choice {choice!r}")
    key = sound_key(name)
    if choice == "auto":
        project.settings.sound_precache.pop(key, None)
    else:
        project.settings.sound_precache[key] = choice
    project.save()


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


def stock_sound_files(project: Project) -> dict[str, Path]:
    """The stock sounds (precached by the game DLL) found in the game folder
    or the base game next to it — what a similar custom sound can become."""
    if not project.settings.game_dir:
        return {}
    game = Path(project.settings.game_dir)
    roots = [r for r in (game / "sound", game.with_name("valve") / "sound") if r.is_dir()]
    out: dict[str, Path] = {}
    for name in sorted(stock_sounds()):
        for root in roots:
            path = root / name
            if path.is_file():
                out[name] = path
                break
    return out


def find_similar(project: Project, *, similar: bool = True,
                 progress=None) -> list:  # noqa: ANN001 - list[SoundGroup], callback
    """Groups of library sounds that could be one file (see
    :mod:`valve_qc_merger.sound.similar`), with the stock sounds they match."""
    from valve_qc_merger.sound.similar import analyse
    shared = {sound_key(k) for k in project.settings.sound_aliases}
    library = {name: sound_path(project, name) for name in list_sounds(project)
               if sound_key(name) not in shared}  # already playing another one
    users = {k: len(v) for k, v in sound_users(project).items()}
    threshold = None if similar else 2.0  # 2.0: no pair is ever that similar
    groups = analyse(library, stock_sound_files(project), users=users, progress=progress,
                     **({} if threshold is None else {"similar_threshold": threshold}))
    return groups


def share_sounds(project: Project, members: list[str], keeper: str) -> list[str]:
    """Make every one of ``members`` play ``keeper`` (aliases applied when a
    build stages its models). Returns the sounds aliased."""
    done = []
    for name in members:
        if sound_key(name) != sound_key(keeper):
            project.settings.sound_aliases[sound_key(name)] = keeper
            done.append(name)
    project.save()
    return done


def unshare_sounds(project: Project, names: list[str]) -> None:
    for name in names:
        project.settings.sound_aliases.pop(sound_key(name), None)
    project.save()


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
