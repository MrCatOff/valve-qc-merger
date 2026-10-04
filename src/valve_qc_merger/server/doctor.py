"""Problems in a mod folder that break, slow or bloat a ReHLDS server.

Each :class:`Issue` names a file, says what is wrong in one sentence and how
bad it is: ``error`` (a client or the server fails: a missing companion file,
a path over 63 characters, a sound a model plays but nobody ships),
``warning`` (works on Windows, breaks on the Linux server or in some
clients: upper case, spaces, non-ASCII, oversized textures, WAV formats the
engine mangles) or ``info`` (wasted downloads: identical files).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from valve_qc_merger.server.limits import MAX_PATH
from valve_qc_merger.server.scan import FolderInventory, model_refs, scan_folder

TEXTURE_MAX = 512  # GoldSource renderers resample anything larger
SEVERITIES = ("error", "warning", "info")


@dataclass(frozen=True)
class Issue:
    severity: str  # error | warning | info
    category: str  # missing | path | case | texture | sound | duplicate | model
    path: str  # relative to the mod folder
    message: str


def _path_issues(relative: str) -> list[Issue]:
    issues = []
    if len(relative) > MAX_PATH:
        issues.append(Issue("error", "path", relative,
                            f"{len(relative)} characters: precache names stop at "
                            f"{MAX_PATH} — the file will not load"))
    if any(ord(c) > 127 for c in relative):
        issues.append(Issue("warning", "path", relative,
                            "non-ASCII characters: clients on other code pages "
                            "cannot download or open it"))
    elif " " in relative:
        issues.append(Issue("warning", "path", relative,
                            "a space in the path: some downloads and commands cut it"))
    if relative != relative.lower():
        issues.append(Issue("warning", "case", relative,
                            "upper case: the Linux server sees 'A.wav' and 'a.wav' as "
                            "different files — keep paths lower case"))
    return issues


def check_folder(root: Path, *, progress: Callable[[int, int, str], None] | None = None,
                 wav_check: Callable[[Path], list[str]] | None = None) -> list[Issue]:
    """Every problem found under ``root`` (a mod folder such as ``cstrike``);
    ``wav_check`` adds sound-format findings (path -> problem sentences)."""
    inventory = scan_folder(root)
    # the engine also finds files in the base game (valve) and in the
    # downloads folder next to the mod: a model may play a stock sound
    fallback = [scan_folder(other) for other in fallback_folders(root)]
    issues: list[Issue] = []
    models = inventory.of_kind(".mdl")
    companions = {m for m in models if re.search(r"(t|\d\d)\.mdl$", m, re.IGNORECASE)}
    resources = inventory.of_kind(".mdl", ".spr", ".wav", ".bsp", ".bmp", ".tga", ".sc",
                                  ".res", ".txt")
    for relative in resources:
        issues.extend(_path_issues(relative))
    main_models = [m for m in models if m not in companions]
    for done, relative in enumerate(main_models):
        if progress is not None:
            progress(done, len(main_models), relative)
        issues.extend(_model_issues(inventory, relative, fallback))
    if wav_check is not None:
        for relative in inventory.of_kind(".wav"):
            for problem in wav_check(root / relative):
                issues.append(Issue("warning", "sound", relative, problem))
    issues.extend(_duplicates(inventory))
    order = {s: i for i, s in enumerate(SEVERITIES)}
    return sorted(issues, key=lambda i: (order[i.severity], i.category, i.path.lower()))


def fallback_folders(root: Path) -> list[Path]:
    """Folders the engine searches after the mod folder: ``<mod>_downloads``
    and the base game ``valve`` next to it."""
    root = Path(root)
    return [p for p in (root.with_name(root.name + "_downloads"), root.with_name("valve"))
            if p.is_dir() and p.resolve() != root.resolve()]


def _model_issues(inventory: FolderInventory, relative: str,
                  fallback: list[FolderInventory] | None = None) -> list[Issue]:
    refs = model_refs(inventory.root / relative)
    if refs.error:
        return [Issue("error", "model", relative, f"cannot be read: {refs.error}")]
    issues = []
    stem = relative[:-4]
    if refs.needs_texture_file and not inventory.exists(f"{stem}T.mdl"):
        issues.append(Issue("error", "missing", relative,
                            f"textures live in {Path(stem).name}T.mdl, which is missing"))
    for group in range(1, refs.sequence_groups):
        if not inventory.exists(f"{stem}{group:02d}.mdl"):
            issues.append(Issue("error", "missing", relative,
                                f"animations of sequence group {group} live in "
                                f"{Path(stem).name}{group:02d}.mdl, which is missing"))
    for sound, _event in sorted(refs.sounds.items()):
        target = f"sound/{sound}"
        if not inventory.exists(target) and any(f.exists(target) for f in fallback or []):
            continue  # shipped by the base game / the downloads folder
        if not inventory.exists(target):
            issues.append(Issue("error", "missing", relative,
                                f"plays {target}, which is not in the folder"))
        elif inventory.actual(target) != target and inventory.actual(target) is not None:
            issues.append(Issue("warning", "case", relative,
                                f"plays {target} but the file is "
                                f"{inventory.actual(target)} (case differs: the Linux "
                                "server will not find it)"))
    large = [t for t in refs.textures if t.width > TEXTURE_MAX or t.height > TEXTURE_MAX]
    for texture in large:
        issues.append(Issue("warning", "texture", relative,
                            f"texture {texture.name} is {texture.width}×{texture.height}: "
                            f"over {TEXTURE_MAX} is resampled by the renderer"))
    return issues


def _duplicates(inventory: FolderInventory) -> list[Issue]:
    by_size: dict[int, list[str]] = {}
    for relative, size in inventory.files.items():
        if size > 0 and relative.lower().endswith((".wav", ".spr", ".bmp", ".tga")):
            by_size.setdefault(size, []).append(relative)
    issues = []
    for paths in by_size.values():
        if len(paths) < 2:
            continue
        by_hash: dict[str, list[str]] = {}
        for relative in paths:
            digest = hashlib.sha1((inventory.root / relative).read_bytes()).hexdigest()
            by_hash.setdefault(digest, []).append(relative)
        for same in by_hash.values():
            if len(same) > 1:
                same.sort()
                for other in same[1:]:
                    issues.append(Issue("info", "duplicate", other,
                                        f"identical to {same[0]}: one file could serve "
                                        "both (a smaller download)"))
    return issues


def summary(issues: list[Issue]) -> dict[str, int]:
    out = dict.fromkeys(SEVERITIES, 0)
    for issue in issues:
        out[issue.severity] += 1
    return out


__all__ = ["Issue", "SEVERITIES", "TEXTURE_MAX", "check_folder", "summary"]
