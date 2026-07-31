"""GoldSource texture finalisation for the delivery directory, pure Python.

studiomdl's constraints on textures, enforced at export time:

- every material a mesh SMD references must exist next to the QC as a file,
- the file must be a BMP with an **8-bit** indexed palette,
- the material/file name must be ASCII with no spaces and carry the ``.bmp``
  extension — studiomdl may refuse a material with no extension, so a missing
  extension is appended both to the SMD's material lines and to the copied file.

`finalize_textures` normalises the material names inside the exported mesh SMDs
(in place), locates each texture in the input directories (case-insensitive),
copies it into the output directory under the final material name, and
validates the format. Violations that cannot be fixed mechanically (non-ASCII
names, missing files, non-8-bit BMPs) are reported as errors for the gate.
"""

from __future__ import annotations

import dataclasses
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.writers.smd import write_smd_file


@dataclass
class TextureReport:
    """Outcome of texture finalisation for one exported model."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    copied: dict[str, str] = field(default_factory=dict)  # final material -> source file


def _normalise(material: str) -> tuple[str, list[str]]:
    """Final material name plus the fixes applied (extension / spaces)."""
    fixes: list[str] = []
    name = material.strip()
    if " " in name:
        name = name.replace(" ", "_")
        fixes.append("spaces replaced with underscores")
    if not name.lower().endswith(".bmp"):
        name += ".bmp"
        fixes.append("missing .bmp extension appended")
    return name, fixes


def _find_texture(final_name: str, original: str, search_dirs: list[Path]) -> Path | None:
    """Locate the texture file, case-insensitively, under either name."""
    stems = {final_name.lower(), original.lower(), (original + ".bmp").lower()}
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.iterdir()):
            if candidate.is_file() and candidate.name.lower() in stems:
                return candidate
    return None


def _bmp_bits(path: Path) -> int | None:
    """The BMP bit depth, or None if the file is not a BMP."""
    header = path.read_bytes()[:54]
    if len(header) < 30 or header[:2] != b"BM":
        return None
    return int(struct.unpack_from("<H", header, 28)[0])


def finalize_textures(
    out_dir: Path, mesh_smds: dict[str, Path], search_dirs: list[Path]
) -> TextureReport:
    """Normalise materials in the exported mesh SMDs and stage their textures."""
    report = TextureReport()
    for mesh_name, smd_path in sorted(mesh_smds.items()):
        smd = parse_smd_file(smd_path)
        renames: dict[str, str] = {}
        for material in sorted({t.material for t in smd.triangles}):
            if not material.isascii():
                report.errors.append(
                    f"{mesh_name}: material {material!r} is not ASCII; rename the "
                    "texture and re-export the mesh"
                )
                continue
            final, fixes = _normalise(material)
            if final != material:
                renames[material] = final
                report.warnings.append(
                    f"[textures] {mesh_name}: material {material!r} -> {final!r} "
                    f"({'; '.join(fixes)})"
                )
            source = _find_texture(final, material, search_dirs)
            if source is None:
                searched = ", ".join(str(d) for d in search_dirs)
                report.errors.append(
                    f"{mesh_name}: texture for material {material!r} not found "
                    f"(searched: {searched})"
                )
                continue
            bits = _bmp_bits(source)
            if bits != 8:
                kind = "not a BMP" if bits is None else f"{bits}-bit"
                report.errors.append(
                    f"{mesh_name}: texture {source.name} is {kind}; studiomdl "
                    "requires 8-bit indexed BMPs — convert it and re-run"
                )
                continue
            destination = out_dir / final
            if final not in report.copied:
                shutil.copyfile(source, destination)
                report.copied[final] = str(source)
        if renames:
            smd.triangles = [
                dataclasses.replace(t, material=renames.get(t.material, t.material))
                for t in smd.triangles
            ]
            write_smd_file(smd, smd_path)
    return report


__all__ = ["TextureReport", "finalize_textures"]
