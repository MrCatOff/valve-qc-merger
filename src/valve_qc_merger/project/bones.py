"""Bone-hierarchy edits on one decompiled asset (Qt-free).

Every edit is applied to ALL of the model's SMDs (reference meshes and
animations) with the FK-exact operations of
:mod:`~valve_qc_merger.merge_view.skeleton_ops`, and the QC's bone references
(``$attachment``, ``$hbox``, ``$controller``) follow. Each edit re-checks that
the world position of every surviving bone is unchanged in every frame of
every SMD and reports the worst deviation.

- **rename**: names only (studiomdl keeps 31 characters).
- **reparent**: the bone keeps its world pose; cycles are refused.
- **delete**: children fold the bone's transform in (they keep their pose);
  vertices it carried move to its parent, and QC references move to the
  parent (attachment offsets re-expressed in the parent's bind frame) —
  both are warned about, as they stop following the deleted bone's motion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.merge_view.discovery import ModelInput, _resolve_smd, load_model
from valve_qc_merger.merge_view.skeleton_ops import (
    fk_worlds,
    rebind_vertices,
    remove_bones,
    rename_bones,
    reparent_bone,
)
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import Smd
from valve_qc_merger.writers.smd import write_smd_file

NAME_LIMIT = 31
POSE_TOLERANCE = 2e-3
_BONE_LINE_RE = re.compile(
    r'^(?P<head>\s*\$(?:attachment|hbox|controller)\s+\S+\s+)'
    r'(?:"(?P<q>[^"]+)"|(?P<b>[^\s"]+))(?P<tail>.*)$',
    re.IGNORECASE | re.MULTILINE,
)
_ATTACH_RE = re.compile(
    r'^\s*\$attachment\s+(\d+)\s+(?:"([^"]+)"|(\S+))\s+(\S+)\s+(\S+)\s+(\S+)[^\n]*\n?',
    re.IGNORECASE | re.MULTILINE,
)


class BoneEditError(ValueError):
    """The edit is not possible (unknown bone, cycle, clash...)."""


@dataclass
class BoneEditResult:
    files: list[Path] = field(default_factory=list)  # rewritten files
    warnings: list[str] = field(default_factory=list)
    max_pose_deviation: float = 0.0


@dataclass
class _Loaded:
    model: ModelInput
    smds: list[tuple[Smd, Path]]  # unique SMD objects and their files


def _load(directory: Path) -> _Loaded:
    model = load_model(Path(directory), require_anims=False)
    smds: list[tuple[Smd, Path]] = []
    seen: set[Path] = set()
    for stem, smd in model.meshes.items():
        path = _resolve_smd(model.directory, stem)
        if path not in seen:
            seen.add(path)
            smds.append((smd, path))
    for seq in model.sequences:
        if seq.smd is None or seq.name not in model.anims:
            continue
        path = _resolve_smd(model.directory, seq.smd)
        if path not in seen:
            seen.add(path)
            smds.append((model.anims[seq.name], path))
    return _Loaded(model, smds)


def _names(loaded: _Loaded) -> list[str]:
    fullest = max(loaded.model.meshes.values(), key=lambda m: len(m.nodes))
    return [n.name for n in fullest.nodes]


def _snapshot(smd: Smd) -> list[dict[str, Vector3]]:
    names = {n.index: n.name for n in smd.nodes}
    return [{names[i]: t.translation for i, t in fk_worlds(smd, f).items()}
            for f in smd.frames]


def _deviation(before: list[dict[str, Vector3]], smd: Smd,
               renames: dict[str, str] | None = None) -> float:
    renames = renames or {}
    worst = 0.0
    for old, new in zip(before, _snapshot(smd), strict=True):
        for name, pos in old.items():
            got = new.get(renames.get(name, name))
            if got is None:
                continue
            worst = max(worst, abs(got.x - pos.x), abs(got.y - pos.y), abs(got.z - pos.z))
    return worst


def _write(loaded: _Loaded, result: BoneEditResult, qc_text: str | None = None) -> None:
    for smd, path in loaded.smds:
        write_smd_file(smd, path)
        result.files.append(path)
    if qc_text is not None and qc_text != loaded.model.qc_text:
        loaded.model.qc_path.write_text(qc_text, encoding="latin-1")
        result.files.append(loaded.model.qc_path)


def _qc_rename(qc: str, mapping: dict[str, str]) -> str:
    def sub(m: re.Match[str]) -> str:
        bone = m.group("q") or m.group("b")
        new = mapping.get(bone, bone)
        return f'{m.group("head")}"{new}"{m.group("tail")}'
    return _BONE_LINE_RE.sub(sub, qc)


# --------------------------------------------------------------------------- #
# Edits
# --------------------------------------------------------------------------- #
def rename_bone(directory: Path, old: str, new: str) -> BoneEditResult:
    loaded = _load(directory)
    names = _names(loaded)
    new = new.strip()
    if old not in names:
        raise BoneEditError(f"no bone {old!r}")
    if not new or '"' in new or len(new) > NAME_LIMIT:
        raise BoneEditError(f"bone names need 1..{NAME_LIMIT} characters, no quotes")
    if new != old and new in names:
        raise BoneEditError(f"a bone named {new!r} already exists")
    result = BoneEditResult()
    for smd, _path in loaded.smds:
        rename_bones(smd, {old: new})
    _write(loaded, result, _qc_rename(loaded.model.qc_text, {old: new}))
    return result


def reparent(directory: Path, bone: str, parent: str | None) -> BoneEditResult:
    loaded = _load(directory)
    names = _names(loaded)
    if bone not in names or (parent is not None and parent not in names):
        raise BoneEditError(f"unknown bone: {bone if bone not in names else parent!r}")
    if parent == bone:
        raise BoneEditError("a bone cannot be its own parent")
    result = BoneEditResult()
    for smd, _path in loaded.smds:
        present = {n.name for n in smd.nodes}
        if bone not in present or (parent is not None and parent not in present):
            continue
        before = _snapshot(smd)
        try:
            reparent_bone(smd, bone, parent)
        except ValueError as exc:
            raise BoneEditError(str(exc)) from exc
        result.max_pose_deviation = max(result.max_pose_deviation, _deviation(before, smd))
    _write(loaded, result)
    return result


def delete_bone(directory: Path, bone: str) -> BoneEditResult:
    loaded = _load(directory)
    names = _names(loaded)
    if bone not in names:
        raise BoneEditError(f"no bone {bone!r}")
    fullest = max(loaded.model.meshes.values(), key=lambda m: len(m.nodes))
    name_of = {n.index: n.name for n in fullest.nodes}
    node = next(n for n in fullest.nodes if n.name == bone)
    parent = name_of.get(node.parent) if node.parent >= 0 else None
    result = BoneEditResult()
    # bind worlds BEFORE the edit (re-expressing attachments needs the bone)
    bind = fk_worlds(fullest, fullest.frames[0]) if fullest.frames else {}
    bind_world = {name_of[i]: t for i, t in bind.items()}

    moved = 0
    for smd, _path in loaded.smds:
        if bone not in {n.name for n in smd.nodes}:
            continue
        if smd.triangles and any(n.name == bone for n in smd.nodes):
            carried = sum(1 for t in smd.triangles for v in t.vertices
                          if v.bone == next(n.index for n in smd.nodes if n.name == bone))
            if carried:
                if parent is None:
                    raise BoneEditError(
                        f"{bone!r} is a root carrying {carried} vertices; reparent it "
                        "or its vertices first")
                moved += rebind_vertices(smd, bone, parent)
        before = _snapshot(smd)
        remove_bones(smd, {bone})
        result.max_pose_deviation = max(result.max_pose_deviation, _deviation(before, smd))
    if moved:
        result.warnings.append(f"{moved} vertices moved from {bone!r} to {parent!r}; they "
                               f"no longer follow {bone!r}'s own motion")

    # QC references follow to the parent
    qc = loaded.model.qc_text
    if parent is None and _BONE_LINE_RE.search(qc) and any(
            (m.group("q") or m.group("b")) == bone for m in _BONE_LINE_RE.finditer(qc)):
        raise BoneEditError(f"QC lines reference root {bone!r}; edit them first")
    def move_attachment(m: re.Match[str]) -> str:
        if (m.group(2) or m.group(3)) != bone:
            return m.group(0)
        offset = Vector3(float(m.group(4)), float(m.group(5)), float(m.group(6)))
        point = bind_world[bone].transform_point(offset)
        local = bind_world[parent].inverse().transform_point(point)
        result.warnings.append(f"$attachment {m.group(1)} moved to {parent!r}")
        end = "\n" if m.group(0).endswith("\n") else ""
        return (f'$attachment {m.group(1)} "{parent}" {local.x:.6f} {local.y:.6f} '
                f"{local.z:.6f}{end}")

    if bind_world and parent is not None:
        qc = _ATTACH_RE.sub(move_attachment, qc)
    for m in _BONE_LINE_RE.finditer(qc):
        if (m.group("q") or m.group("b")) == bone:
            result.warnings.append(f"{m.group(0).strip()!r} now uses {parent!r} "
                                   "(box/controller kept as is)")
    qc = _qc_rename(qc, {bone: parent}) if parent is not None else qc
    _write(loaded, result, qc)
    return result


# --------------------------------------------------------------------------- #
# Attachments
# --------------------------------------------------------------------------- #
@dataclass
class AttachmentSpec:
    index: int
    bone: str
    offset: tuple[float, float, float]


def read_attachments(directory: Path) -> list[AttachmentSpec]:
    # the same QC load_model() reads (a folder may hold several)
    qc = sorted(Path(directory).glob("*.qc"))[0].read_text(encoding="latin-1")
    return [AttachmentSpec(int(m.group(1)), m.group(2) or m.group(3),
                           (float(m.group(4)), float(m.group(5)), float(m.group(6))))
            for m in _ATTACH_RE.finditer(qc)]


def write_attachments(directory: Path, specs: list[AttachmentSpec]) -> BoneEditResult:
    """Replace every ``$attachment`` line (written where the first one was,
    else before the first ``$sequence``)."""
    loaded = _load(directory)
    names = set(_names(loaded))
    result = BoneEditResult()
    for spec in specs:
        if spec.bone not in names:
            raise BoneEditError(f"attachment {spec.index}: no bone {spec.bone!r}")
    if len({s.index for s in specs}) != len(specs):
        raise BoneEditError("attachment indices must be unique")
    if len(specs) > 4:
        result.warnings.append(f"{len(specs)} attachments: GoldSource keeps 4")
    qc = loaded.model.qc_text
    first = _ATTACH_RE.search(qc)
    lines = "".join(f'$attachment {s.index} "{s.bone}" {s.offset[0]:g} {s.offset[1]:g} '
                    f"{s.offset[2]:g}\n" for s in sorted(specs, key=lambda s: s.index))
    stripped = _ATTACH_RE.sub("", qc)
    if first is not None:
        at = first.start()
    else:
        seq = re.search(r"^\s*\$sequence\b", stripped, re.MULTILINE | re.IGNORECASE)
        at = seq.start() if seq else len(stripped)
        lines += "\n" if lines else ""
    qc = stripped[:at] + lines + stripped[at:]
    loaded.model.qc_path.write_text(qc, encoding="latin-1")
    result.files.append(loaded.model.qc_path)
    return result


__all__ = [
    "AttachmentSpec",
    "BoneEditError",
    "BoneEditResult",
    "delete_bone",
    "read_attachments",
    "rename_bone",
    "reparent",
    "write_attachments",
]
