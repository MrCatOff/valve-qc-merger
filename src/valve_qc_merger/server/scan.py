"""What a mod folder holds and what its models reference — read fast.

:func:`model_refs` reads only a model's header, its texture headers and its
sequence events (no geometry, no pixels), so a whole ``cstrike`` folder of
a thousand models scans in seconds. :func:`scan_folder` lists the files a
server precaches or sends (models, sprites, sounds, maps) with their sizes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

# events whose option names a sound file
SOUND_EVENTS = {5004: "client sound", 1004: "sound", 1008: "voice"}
RESOURCE_DIRS = ("models", "sprites", "sound", "maps", "gfx", "events", "overviews")


@dataclass
class TextureRef:
    name: str
    width: int
    height: int


@dataclass
class ModelRefs:
    path: Path
    version: int
    textures: list[TextureRef] = field(default_factory=list)
    needs_texture_file: bool = False  # textures live in <name>T.mdl
    sequence_groups: int = 1  # > 1: <name>01.mdl, 02 ... hold animations
    sounds: dict[str, int] = field(default_factory=dict)  # sound path -> event id
    error: str = ""


def model_refs(path: Path) -> ModelRefs:
    """Header-level references of one ``.mdl`` (v9/v10)."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        return ModelRefs(path, 0, error=str(exc))
    if len(data) < 244 or data[:4] != b"IDST":
        return ModelRefs(path, 0, error="not a GoldSource model (IDST)")
    (version,) = struct.unpack_from("<i", data, 4)
    refs = ModelRefs(path, version)
    if version not in (9, 10):
        refs.error = f"studio version {version}"
        return refs
    fields = struct.unpack_from("<21i", data, 136)
    numseq, seqindex, numseqgroups = fields[7], fields[8], fields[9]
    numtextures, textureindex = fields[11], fields[12]
    try:
        for i in range(numtextures):
            off = textureindex + i * 80
            name = data[off:off + 64].split(b"\0", 1)[0].decode("latin-1")
            _flags, width, height = struct.unpack_from("<3i", data, off + 64)
            refs.textures.append(TextureRef(name, width, height))
        refs.needs_texture_file = numtextures == 0
        refs.sequence_groups = max(numseqgroups, 1)
        for i in range(numseq):
            off = seqindex + i * 176
            numevents, eventindex = struct.unpack_from("<2i", data, off + 48)
            for e in range(numevents):
                eoff = eventindex + e * 76
                event = struct.unpack_from("<i", data, eoff + 4)[0]
                if event in SOUND_EVENTS:
                    option = data[eoff + 12:eoff + 76].split(b"\0", 1)[0]
                    sound = option.decode("latin-1").strip().replace("\\", "/")
                    if sound and not sound.startswith("!"):
                        refs.sounds.setdefault(sound, event)
    except struct.error:
        refs.error = "truncated model"
    return refs


@dataclass
class FolderInventory:
    root: Path
    files: dict[str, int] = field(default_factory=dict)  # relative posix path -> bytes

    def of_kind(self, *suffixes: str) -> list[str]:
        return sorted(p for p in self.files if p.lower().endswith(suffixes))

    def exists(self, relative: str) -> bool:
        """Case-insensitive, like the Windows client (the Linux server is not)."""
        return relative.lower() in self._lower

    def actual(self, relative: str) -> str | None:
        """The path as spelled on disk for a case-insensitive match."""
        return self._lower.get(relative.lower())

    def __post_init__(self) -> None:
        self._lower = {p.lower(): p for p in self.files}

    def refresh_index(self) -> None:
        self._lower = {p.lower(): p for p in self.files}


def scan_folder(root: Path) -> FolderInventory:
    """Every file under the resource folders of a mod folder (``models``,
    ``sprites``, ``sound``, ``maps``, ``gfx``, ``events``) and its texture
    WADs (``*.wad`` at the top: what maps name in their worldspawn)."""
    root = Path(root)
    inventory = FolderInventory(root)
    if root.is_dir():
        for path in root.glob("*"):
            if path.is_file() and path.suffix.lower() == ".wad":
                inventory.files[path.name] = path.stat().st_size
    for top in RESOURCE_DIRS:
        base = root / top
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file():
                inventory.files[path.relative_to(root).as_posix()] = path.stat().st_size
    inventory.refresh_index()
    return inventory


__all__ = ["FolderInventory", "ModelRefs", "SOUND_EVENTS", "TextureRef", "model_refs",
           "scan_folder"]
