"""Model discovery, filename sanitisation and loading (merge-view M1, spec §3.1–3.2).

Pure Python over the typed QC/SMD layer. A *model* is a directory containing
exactly one ``.qc``; its manifest (bodygroups, sequences with SMD paths,
attachments) comes from that QC. Decompiled corpora carry non-ASCII file names
(Korean/Japanese leftovers) and extensionless BMP textures; both are fixed on
disk with every textual reference patched, before anything is parsed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.models.smd import Smd
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget.qc_build import QcSequence, parse_bodygroups, parse_sequences


class MergeViewError(RuntimeError):
    """A merge-view failure with a model-level diagnostic."""


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def discover_models(root: Path, *, exclude: set[str] | None = None) -> list[Path]:
    """Every direct subdirectory of ``root`` holding exactly one ``.qc``.

    Deterministic (sorted by name). Directories with zero QCs are skipped
    silently (texture dumps, anims-only folders); more than one QC is an error
    naming the directory — ambiguous manifests must be resolved by hand.
    """
    if not root.is_dir():
        raise MergeViewError(f"models directory not found: {root}")
    excluded = exclude or set()
    models: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in excluded:
            continue
        qcs = sorted(child.glob("*.qc"))
        if not qcs:
            continue
        if len(qcs) > 1:
            raise MergeViewError(
                f"model {child.name!r} has {len(qcs)} .qc files; expected exactly one: "
                f"{[q.name for q in qcs]}"
            )
        models.append(child)
    if not models:
        raise MergeViewError(f"no model directories (containing one .qc) under {root}")
    return models


# --------------------------------------------------------------------------- #
# Filename sanitisation
# --------------------------------------------------------------------------- #
_TEXT_SUFFIXES = {".qc", ".smd"}


def _ascii_name(name: str, taken: set[str]) -> str:
    """Collision-free ASCII replacement for a non-ASCII file name."""
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    cleaned = re.sub(r"[^\x20-\x7e]", "", stem).replace(" ", "_").strip("_") or "noname"
    candidate = f"{cleaned}.{suffix}" if suffix else cleaned
    counter = 1
    while candidate.lower() in taken:
        counter += 1
        candidate = f"{cleaned}_{counter}.{suffix}" if suffix else f"{cleaned}_{counter}"
    return candidate


def sanitize_model_dir(model_dir: Path) -> dict[str, str]:
    """Rename non-ASCII files to ASCII and patch every textual reference.

    Returns ``{old_name: new_name}``. References inside every ``.qc``/``.smd``
    are replaced case-insensitively (decompilers disagree on case), then the
    physical files are renamed.
    """
    renames: dict[str, str] = {}
    taken = {p.name.lower() for p in model_dir.rglob("*") if p.is_file()}
    for path in sorted(model_dir.rglob("*")):
        if path.is_file() and not path.name.isascii():
            new = _ascii_name(path.name, taken)
            taken.add(new.lower())
            renames[path.name] = new
    if not renames:
        return renames

    # Patch references at BYTE level: the file names are Unicode, but the file
    # contents carry them in whatever encoding the decompiler used (UTF-8,
    # CP949, Shift-JIS...), so try each plausible byte form of the old name.
    for text_file in sorted(model_dir.rglob("*")):
        if not (text_file.is_file() and text_file.suffix.lower() in _TEXT_SUFFIXES):
            continue
        raw = text_file.read_bytes()
        patched = raw
        for old, new in renames.items():
            for encoding in ("utf-8", "cp949", "shift_jis", "latin-1"):
                try:
                    needle = old.encode(encoding)
                except UnicodeEncodeError:
                    continue
                patched = patched.replace(needle, new.encode("ascii"))
        if patched != raw:
            text_file.write_bytes(patched)

    for path in sorted(model_dir.rglob("*")):
        if path.is_file() and path.name in renames:
            path.rename(path.with_name(renames[path.name]))
    return renames


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
@dataclass
class ModelInput:
    """One decompiled model, fully parsed."""

    name: str  # directory name (v_anaconda)
    directory: Path
    qc_path: Path
    qc_text: str
    bodygroups: dict[str, list[str]]  # group name -> studio stems
    sequences: list[QcSequence]
    meshes: dict[str, Smd] = field(default_factory=dict)  # studio stem -> parsed SMD
    anims: dict[str, Smd] = field(default_factory=dict)  # sequence name -> parsed SMD
    warnings: list[str] = field(default_factory=list)

    @property
    def bone_names(self) -> list[str]:
        """Node names from the fullest mesh (reference SMDs carry the rig)."""
        best = max(self.meshes.values(), key=lambda m: len(m.nodes), default=None)
        return [n.name for n in best.nodes] if best is not None else []


def _resolve_smd(model_dir: Path, stem: str) -> Path:
    relative = stem.replace("\\", "/")
    if not relative.lower().endswith(".smd"):
        relative += ".smd"
    return model_dir / relative


def load_model(model_dir: Path, *, require_anims: bool = True) -> ModelInput:
    """Parse one model's QC manifest and every SMD it references.

    ``require_anims=False`` tolerates QCs with no ``$sequence`` blocks at all
    (some decompiled p_ models ship without one); view-model merging always
    requires animations.
    """
    qc_path = sorted(model_dir.glob("*.qc"))[0]
    qc_text = qc_path.read_text(encoding="latin-1")
    bodygroups = parse_bodygroups(qc_text)
    sequences = parse_sequences(qc_text)
    model = ModelInput(
        name=model_dir.name, directory=model_dir, qc_path=qc_path, qc_text=qc_text,
        bodygroups=bodygroups, sequences=sequences,
    )

    for group, stems in bodygroups.items():
        for stem in stems:
            path = _resolve_smd(model_dir, stem)
            if not path.exists():
                model.warnings.append(f"bodygroup {group!r} studio missing: {path.name}")
                continue
            if stem not in model.meshes:
                model.meshes[stem] = parse_smd_file(path)

    for seq in sequences:
        if seq.smd is None:
            model.warnings.append(f"sequence {seq.name!r} has no SMD path in the QC")
            continue
        path = _resolve_smd(model_dir, seq.smd)
        if not path.exists():
            model.warnings.append(f"sequence {seq.name!r} SMD missing: {path}")
            continue
        model.anims[seq.name] = parse_smd_file(path)

    if not model.meshes:
        raise MergeViewError(f"model {model.name!r}: no mesh SMDs resolved from the QC")
    if require_anims and not model.anims:
        raise MergeViewError(f"model {model.name!r}: no animation SMDs resolved from the QC")
    return model


__all__ = [
    "MergeViewError",
    "ModelInput",
    "discover_models",
    "sanitize_model_dir",
    "load_model",
]
