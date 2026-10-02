"""What the studio Inspector shows about one decompiled model (Qt-free).

Reads the asset folder (QC + SMDs) once and summarises it: bodygroups with
per-submodel triangle/vertex counts, textures with size and render mode,
sequences, bones (with vertex counts) and attachments.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.merge_view.bmp8 import BmpError, read_bmp8
from valve_qc_merger.merge_view.discovery import ModelInput, load_model

_ATTACH_RE = re.compile(
    r'^\s*\$attachment\s+(\d+)\s+(?:"([^"]+)"|(\S+))\s+(\S+)\s+(\S+)\s+(\S+)',
    re.MULTILINE | re.IGNORECASE,
)
_RENDERMODE_RE = re.compile(r'^\s*\$texrendermode\s+"?([^"\r\n]+?)"?\s+(\w+)\s*$',
                            re.MULTILINE | re.IGNORECASE)
_GROUP_ENTRY_RE = re.compile(r'studio\s+"(?P<stem>[^"]+)"|\bblank\b', re.IGNORECASE)


def _entries_with_blanks(qc: str, groups: dict[str, list[str]]) -> dict[str, list[str]]:
    """Each bodygroup's entries in file order INCLUDING ``blank`` (the shared
    parser keeps studio stems only; blanks matter for pev_body indices)."""
    from valve_qc_merger.retarget.qc_build import _BODYGROUP_RE, _matching_brace

    out: dict[str, list[str]] = {}
    names = iter(groups)
    found: list[tuple[int, list[str]]] = []
    for m in _BODYGROUP_RE.finditer(qc):
        body = qc[m.end():_matching_brace(qc, m.end() - 1)]
        found.append((m.start(), [e.group("stem") or "blank"
                                  for e in _GROUP_ENTRY_RE.finditer(body)]))
    body_re = re.compile(r'\$body\s+(?:"[^"]+"|[^\s{}"]+)\s+"(?P<stem>[^"]+)"')
    found += [(m.start(), [m.group("stem")]) for m in body_re.finditer(qc)]
    for _pos, entries in sorted(found, key=lambda item: item[0]):
        name = next(names, None)
        if name is None:
            break
        out[name] = entries
    return out


@dataclass
class SubmodelInfo:
    group: str
    stem: str  # "blank" for an empty entry
    triangles: int = 0
    vertices: int = 0
    materials: list[str] = field(default_factory=list)


@dataclass
class TextureInfo:
    name: str
    path: Path | None
    width: int = 0
    height: int = 0
    render_mode: str = ""
    used_by: list[str] = field(default_factory=list)


@dataclass
class SequenceInfo:
    index: int
    name: str
    fps: float | None
    frames: int
    loop: bool
    events: tuple[str, ...]


@dataclass
class BoneInfo:
    index: int
    name: str
    parent: int
    vertices: int


@dataclass
class AttachmentInfo:
    index: int
    bone: str
    offset: tuple[float, float, float]


@dataclass
class ModelInfo:
    name: str
    directory: Path
    qc_path: Path
    submodels: list[SubmodelInfo]
    textures: list[TextureInfo]
    sequences: list[SequenceInfo]
    bones: list[BoneInfo]
    attachments: list[AttachmentInfo]
    warnings: list[str]

    @property
    def triangles(self) -> int:
        return sum(s.triangles for s in self.submodels)


def _find_texture(directory: Path, name: str) -> Path | None:
    wanted = name.lower()
    for candidate in directory.rglob("*"):
        if candidate.is_file() and candidate.name.lower() == wanted:
            return candidate
    return None


def read_model_info(directory: Path, model: ModelInput | None = None) -> ModelInfo:
    """Summarise the decompiled model in ``directory`` (one ``.qc``); pass an
    already loaded ``model`` to avoid parsing it twice."""
    model = model or load_model(Path(directory), require_anims=False)
    qc = model.qc_text

    submodels: list[SubmodelInfo] = []
    texture_users: dict[str, set[str]] = {}
    for group, stems in _entries_with_blanks(qc, model.bodygroups).items():
        for stem in stems:
            if stem.lower() == "blank" or stem not in model.meshes:
                submodels.append(SubmodelInfo(group, stem if stem in model.meshes else "blank"))
                continue
            mesh = model.meshes[stem]
            verts = {(v.bone, v.position) for t in mesh.triangles for v in t.vertices}
            materials = sorted({t.material for t in mesh.triangles})
            for material in materials:
                texture_users.setdefault(material.lower(), set()).add(stem)
            submodels.append(SubmodelInfo(group, stem, len(mesh.triangles), len(verts),
                                          materials))

    modes = {name.lower(): mode for name, mode in _RENDERMODE_RE.findall(qc)}
    textures: list[TextureInfo] = []
    seen: set[str] = set()
    for submodel in submodels:
        for material in submodel.materials:
            key = material.lower()
            if key in seen:
                continue
            seen.add(key)
            path = _find_texture(model.directory, material)
            info = TextureInfo(material, path, render_mode=modes.get(key, ""),
                               used_by=sorted(texture_users.get(key, ())))
            if path is not None:
                try:
                    image = read_bmp8(path.read_bytes())
                    info.width, info.height = image.width, image.height
                except (BmpError, OSError) as exc:
                    model.warnings.append(f"texture {material}: {exc}")
            else:
                model.warnings.append(f"texture {material} not found")
            textures.append(info)

    sequences = [
        SequenceInfo(i, seq.name, seq.fps,
                     len(model.anims[seq.name].frames) if seq.name in model.anims else 0,
                     seq.loop, seq.events)
        for i, seq in enumerate(model.sequences)
    ]

    fullest = max(model.meshes.values(), key=lambda m: len(m.nodes), default=None)
    weights: Counter[str] = Counter()
    for mesh in model.meshes.values():
        names = {n.index: n.name for n in mesh.nodes}
        for t in mesh.triangles:
            for v in t.vertices:
                weights[names[v.bone]] += 1
    bones = ([BoneInfo(n.index, n.name, n.parent, weights[n.name]) for n in fullest.nodes]
             if fullest is not None else [])

    attachments = [
        AttachmentInfo(int(m.group(1)), m.group(2) or m.group(3),
                       (float(m.group(4)), float(m.group(5)), float(m.group(6))))
        for m in _ATTACH_RE.finditer(qc)
    ]
    return ModelInfo(model.name, model.directory, model.qc_path, submodels, textures,
                     sequences, bones, attachments, list(model.warnings))


def texture_rgba(path: Path, *, masked: bool = False) -> tuple[int, int, bytes]:
    """(width, height, RGBA bytes) of an 8-bit BMP; with ``masked`` palette
    index 255 is transparent (GoldSource ``masked`` render mode)."""
    image = read_bmp8(path.read_bytes())
    lut = [bytes((r, g, b, 255)) for r, g, b in image.palette]
    if masked:
        lut[255] = bytes((0, 0, 0, 0))
    return image.width, image.height, b"".join(lut[i] for i in image.pixels)


__all__ = [
    "AttachmentInfo",
    "BoneInfo",
    "ModelInfo",
    "SequenceInfo",
    "SubmodelInfo",
    "TextureInfo",
    "read_model_info",
    "texture_rgba",
]
