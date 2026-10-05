"""What a new player downloads, how long it takes, and how to make it less.

:func:`package_files` lists the files the project's server package holds
(compiled builds with their companions and manifests, the client sounds
their models play, the sprite library) without exporting anything.
:func:`suggestions` reads them for bytes that buy nothing:

- textures no skin of the model uses (studiomdl packs every texture of the
  QC, used or not);
- textures over 512 px — the renderer resamples them down anyway;
- 44.1 kHz or stereo sounds — 22.05 kHz mono is half or a quarter the size
  and what the engine mixes at;
- identical files under two names (one could serve both).

Speeds are rough: FastDL is a web download (megabytes a second); without
it the engine sends files itself in small packets (tens of kilobytes a
second, ``sv_rehlds_force_dlmax 1`` helps).
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from valve_qc_merger.project.model import Project

FASTDL_BPS = 2 * 1024 * 1024
INGAME_BPS = 30 * 1024
TEXTURE_MAX = 512


@dataclass
class Suggestion:
    path: str
    kind: str  # texture | sound | duplicate
    message: str
    saving: int  # bytes, estimated


def package_files(project: Project) -> dict[str, Path]:
    """Mod-relative path -> source file of everything the package would hold."""
    from valve_qc_merger.project import sounds as library
    from valve_qc_merger.project import sprites as sprite_library
    from valve_qc_merger.project.model import ProjectError
    from valve_qc_merger.server.budget import _CLIENT_SOUND
    out: dict[str, Path] = {}
    marker = Path("/__vqm_mod__")
    sounds: set[str] = set()
    for name in sorted(project.builds):
        try:
            pairs = project.deploy_files(name, root=marker)
        except ProjectError:
            continue
        for source, destination in pairs:
            out[destination.relative_to(marker).as_posix()] = source
        for qc in (project.build_dir(name) / "output").rglob("*.qc"):
            sounds.update(s.replace("\\", "/") for s in
                          _CLIENT_SOUND.findall(qc.read_text(encoding="latin-1")))
    for sound in sorted(sounds, key=str.lower):
        source = library.resolve(project, sound)
        if source is not None:
            out[f"sound/{sound}"] = source
    for name in sprite_library.list_sprites(project):
        out[f"sprites/{name}"] = sprite_library.sprite_path(project, name)
    return out


def human(count: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "bytes" else f"{count:.1f} {unit}"
        count /= 1024
    return str(count)


def duration(size: int, bytes_per_second: float) -> str:
    seconds = size / bytes_per_second
    if seconds < 60:
        return f"{max(seconds, 1):.0f} s"
    return f"{seconds / 60:.0f} min" if seconds < 3600 else f"{seconds / 3600:.1f} h"


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
def _studio(data: bytes) -> tuple[int, ...] | None:
    if len(data) < 244 or data[:4] != b"IDST":
        return None
    if struct.unpack_from("<i", data, 4)[0] not in (9, 10):
        return None
    return struct.unpack_from("<21i", data, 136)


def _textures(data: bytes, h: tuple[int, ...]) -> list[tuple[str, int, int]]:
    out = []
    for i in range(h[11]):
        off = h[12] + i * 80
        name = data[off:off + 64].split(b"\0", 1)[0].decode("latin-1")
        _flags, width, height = struct.unpack_from("<3i", data, off + 64)
        out.append((name, width, height))
    return out


def _used_skinrefs(data: bytes, h: tuple[int, ...]) -> set[int]:
    used = set()
    for b in range(h[17]):
        boff = h[18] + b * 76
        nummodels, _base, modelindex = struct.unpack_from("<3i", data, boff + 64)
        for m in range(nummodels):
            moff = modelindex + m * 112
            nummesh, meshindex = struct.unpack_from("<2i", data, moff + 72)
            for k in range(nummesh):
                used.add(struct.unpack_from("<i", data, meshindex + k * 20 + 8)[0])
    return used


def model_suggestions(path: Path, relative: str) -> list[Suggestion]:
    """Unused and oversized textures of a compiled model (textures and skins
    read from its ``T.mdl`` when it has one)."""
    try:
        data = Path(path).read_bytes()
        header = _studio(data)
        if header is None:
            return []
        used_refs = _used_skinrefs(data, header)
        tex_data, tex_header = data, header
        texture_file = Path(path).with_name(f"{Path(path).stem}T.mdl")
        if header[11] == 0 and texture_file.is_file():
            tex_data = texture_file.read_bytes()
            tex_header = _studio(tex_data)
            if tex_header is None:
                return []
        textures = _textures(tex_data, tex_header)
        numskinref, families, skinindex = tex_header[14], tex_header[15], tex_header[16]
        used: set[int] = set()
        for family in range(max(families, 1)):
            for ref in used_refs:
                if 0 <= ref < numskinref:
                    used.add(struct.unpack_from("<h", tex_data,
                                                skinindex + (family * numskinref + ref) * 2)[0])
    except (OSError, struct.error):
        return []
    out = []
    unused = [(n, w, h) for i, (n, w, h) in enumerate(textures) if i not in used]
    if unused and used_refs:
        out.append(Suggestion(relative, "texture",
                              f"{len(unused)} texture(s) no skin uses: "
                              + ", ".join(n for n, _w, _h in unused[:6]),
                              sum(w * h + 768 for _n, w, h in unused)))
    for name, width, height in textures:
        if (width > TEXTURE_MAX or height > TEXTURE_MAX) and name not in \
                {n for n, _w, _h in unused}:
            scale = TEXTURE_MAX / max(width, height)
            saving = width * height - int(width * scale) * int(height * scale)
            out.append(Suggestion(relative, "texture",
                                  f"{name} is {width}×{height}: the renderer resamples it "
                                  f"to {TEXTURE_MAX} anyway", saving))
    return out


# --------------------------------------------------------------------------- #
# sounds, duplicates
# --------------------------------------------------------------------------- #
def sound_suggestions(path: Path, relative: str) -> list[Suggestion]:
    from valve_qc_merger.sound.wav import WavError, read_wav
    try:
        wav = read_wav(Path(path))
    except (WavError, OSError, ValueError):
        return []
    size = Path(path).stat().st_size
    factor = 1.0
    reasons = []
    if wav.rate > 22050:
        factor *= 22050 / wav.rate
        reasons.append(f"{wav.rate} Hz → 22050")
    if wav.channels > 1:
        factor /= wav.channels
        reasons.append("stereo → mono")
    if not reasons:
        return []
    return [Suggestion(relative, "sound", ", ".join(reasons) + " (Sounds ▸ Fix)",
                       int(size * (1 - factor)))]


def duplicate_suggestions(files: dict[str, Path]) -> list[Suggestion]:
    by_hash: dict[str, list[str]] = {}
    for relative, path in files.items():
        if re.search(r"(?i)\.(ini|json|toml|txt)$", relative):
            continue
        try:
            digest = hashlib.md5(Path(path).read_bytes()).hexdigest()
        except OSError:
            continue
        by_hash.setdefault(digest, []).append(relative)
    out = []
    for paths in by_hash.values():
        if len(paths) > 1:
            paths.sort(key=str.lower)
            size = Path(files[paths[0]]).stat().st_size
            out.append(Suggestion(paths[0], "duplicate", "the same file as "
                                  + ", ".join(paths[1:]), size * (len(paths) - 1)))
    return out


def suggestions(files: dict[str, Path]) -> list[Suggestion]:
    """Every suggestion for ``files`` (path -> file), biggest saving first."""
    out: list[Suggestion] = []
    for relative, path in files.items():
        low = relative.lower()
        if low.endswith(".mdl") and not re.search(r"(t|\d\d)\.mdl$", low):
            out += model_suggestions(path, relative)
        elif low.endswith(".wav"):
            out += sound_suggestions(path, relative)
    out += duplicate_suggestions(files)
    return sorted(out, key=lambda s: (-s.saving, s.path.lower()))


__all__ = ["FASTDL_BPS", "INGAME_BPS", "Suggestion", "duplicate_suggestions", "duration",
           "human", "model_suggestions", "package_files", "sound_suggestions",
           "suggestions"]
