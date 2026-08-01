"""Model merging for merge-world.

Emits one compilable part: per-weapon geometry SMDs whose (pre-baked)
vertices all ride ONE canonical ``weapon`` bone under a ``flash`` root — so
studiomdl auto-generates a single hitbox that always covers the visible
weapon — plus a single-frame identity ``idle`` sequence, staged textures with
the column-merged ``$texturegroup``, and a QC whose single ``weapons``
bodygroup starts with ``blank`` (``pev_body 0`` shows nothing, weapon *i* is
``pev_body i+1``).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from valve_qc_merger.merge_player.merger import (
    _stage_skin_variants,
    parse_texturegroups,
)
from valve_qc_merger.merge_view.atlas import (
    TextureOptions,
    downscale_textures,
    pack_textures,
)
from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.merge_view.merger import (
    STOCK_VERT_LIMIT,
    SUBMODEL_LIMIT,
    SUBMODEL_TRI_WARN,
    TEXTURE_WARN,
    MergeError,
    MergeReport,
    _collect_render_modes,
    _concat_meshes,
    _stage_textures,
    write_manifest_data,
)
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd
from valve_qc_merger.writers.smd import write_smd_text

FLASH_BONE = "flash"
WEAPON_BONE = "weapon"

_ZERO = Vector3(0.0, 0.0, 0.0)


def _canonical_nodes() -> list[Node]:
    return [Node(0, FLASH_BONE, -1), Node(1, WEAPON_BONE, 0)]


def _identity_frame() -> Frame:
    return Frame(0, (BonePose(0, _ZERO, _ZERO), BonePose(1, _ZERO, _ZERO)))


def merge_world_models(
    models: list[ModelInput],
    out_dir: Path,
    name: str,
    *,
    manifest_format: str = "ini",
    write_manifest: bool = True,
    textures: TextureOptions | None = None,
) -> MergeReport:
    """Write one merged w_ model part; returns the budget report."""
    report = MergeReport()
    report.bones = 2
    report.bodyparts = 1

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "geometry").mkdir(exist_ok=True)
    (out_dir / "animations").mkdir(exist_ok=True)

    # --- per-weapon geometry on the canonical two-bone table ---------------
    kept: dict[str, list[Smd]] = {}
    for model in models:
        non_empty = [smd for smd in model.meshes.values() if smd.triangles]
        if not non_empty:
            raise MergeError(f"model {model.name!r}: every mesh SMD is empty")
        if len(non_empty) < len(model.meshes):
            report.warnings.append(
                f"{model.name}: dropped "
                f"{len(model.meshes) - len(non_empty)} empty mesh SMDs"
            )
        concat = _concat_meshes(non_empty)
        # Vertices are pre-baked into rendered space; every one of them now
        # rides the single canonical weapon bone.
        triangles = [
            dataclasses.replace(t, vertices=(
                dataclasses.replace(t.vertices[0], bone=1),
                dataclasses.replace(t.vertices[1], bone=1),
                dataclasses.replace(t.vertices[2], bone=1),
            ))
            for t in concat.triangles
        ]
        kept[model.name] = [Smd(
            nodes=_canonical_nodes(), frames=[_identity_frame()],
            triangles=triangles,
        )]

    # --- textures ----------------------------------------------------------
    staged_renames, staged_names = _stage_textures(out_dir, models, kept, report)
    render_modes = _collect_render_modes(
        models, kept, staged_renames, staged_names, report,
    )
    skin_columns, variant_names = _stage_skin_variants(
        out_dir, models, staged_renames, staged_names, report,
    )
    all_staged = staged_names + variant_names
    report.textures = len(all_staged)
    if textures is not None and textures.max_size is not None:
        downscale_textures(out_dir, all_staged, textures.max_size,
                           report.warnings)
    if textures is not None and textures.pack:
        skin_files = sorted({name for column in skin_columns for name in column})
        options = dataclasses.replace(
            textures, no_pack=[*textures.no_pack, *skin_files],
        )
        flat_kept = [smd for smds in kept.values() for smd in smds]
        report.atlas = pack_textures(
            out_dir, flat_kept, staged_names, render_modes, options,
            report.warnings,
        )
        atlas_files = {target.split(":")[0] for target in report.atlas.values()}
        for packed in report.atlas:
            render_modes.pop(packed, None)
        for atlas_file in sorted(atlas_files):
            if atlas_file.startswith("atlasm"):
                render_modes[atlas_file] = "masked"
        report.textures = len(all_staged) - len(report.atlas) + len(atlas_files)
    if report.textures > TEXTURE_WARN:
        report.warnings.append(
            f"{report.textures} textures staged (studiomdl degrades past "
            f"~{TEXTURE_WARN}; lower the texture budget to split further)"
        )

    # --- geometry files -----------------------------------------------------
    submodels = 1 + len(models)  # leading blank
    if submodels > SUBMODEL_LIMIT:
        raise MergeError(
            f"{submodels} submodels in one model exceed studiomdl's hard "
            f"{SUBMODEL_LIMIT}-entry arrays (silent memory corruption); "
            "split into more parts"
        )
    for model in models:
        mesh = kept[model.name][0]
        (out_dir / "geometry" / f"{model.name}.smd").write_text(
            write_smd_text(mesh), encoding="latin-1"
        )
        verts = {v.position for t in mesh.triangles for v in t.vertices}
        norms = {v.normal for t in mesh.triangles for v in t.vertices}
        if len(verts) > STOCK_VERT_LIMIT or len(norms) > STOCK_VERT_LIMIT:
            report.warnings.append(
                f"{model.name}: submodel has {len(verts)} verts / "
                f"{len(norms)} normals (stock studiomdl caps both at "
                f"{STOCK_VERT_LIMIT}; needs a raised-limit compiler)"
            )
        if len(mesh.triangles) > SUBMODEL_TRI_WARN:
            report.warnings.append(
                f"{model.name}: submodel has {len(mesh.triangles)} tris "
                f"(GoldSrc renderers degrade past ~{SUBMODEL_TRI_WARN} "
                "per submodel)"
            )

    # --- idle sequence ------------------------------------------------------
    idle = Smd(nodes=_canonical_nodes(), frames=[_identity_frame()],
               triangles=[])
    (out_dir / "animations" / "idle.smd").write_text(
        write_smd_text(idle), encoding="latin-1"
    )
    report.sequences = 1

    # --- QC ------------------------------------------------------------------
    lines: list[str] = [
        "// Generated by valve-qc-merger merge-world: two bones total, every",
        "// vertex on the weapon bone (single auto hitbox), one weapon per",
        "// pev_body selection (pev_body 0 = blank).",
        "",
        f'$modelname "{name}.mdl"',
        '$cd "."',
        '$cdtexture "."',
        "$cliptotextures",
        "$scale 1.0",
        "",
    ]
    render_lines = [f'$texrendermode "{file}" {mode}'
                    for file, mode in sorted(render_modes.items())]
    if render_lines:
        lines.extend(render_lines)
        lines.append("")
    lines.append('$bodygroup "weapons"')
    lines.append("{")
    lines.append("\tblank")
    for model in models:
        lines.append(f'\tstudio "geometry\\{model.name}"')
    lines.append("}")
    lines.append("")
    if skin_columns:
        max_rows = max(len(column) for column in skin_columns)
        lines.append('$texturegroup "skinfamilies"')
        lines.append("{")
        for row in range(max_rows):
            entries = " ".join(
                f'"{column[min(row, len(column) - 1)]}"'
                for column in skin_columns
            )
            lines.append("\t{ " + entries + " }")
        lines.append("}")
        lines.append("")
    lines.append("$flags 0")
    lines.append("")
    lines.append('$sequence "idle" {')
    lines.append('\t"animations\\idle"')
    lines.append("\tfps 30")
    lines.append("}")
    (out_dir / f"{name}.qc").write_text("\n".join(lines) + "\n", encoding="latin-1")

    for model in models:
        if "$attachment" in model.qc_text:
            report.warnings.append(
                f"{model.name}: attachment(s) dropped (GoldSrc caps a model "
                "at 4 attachments; per-weapon points cannot merge)"
            )

    # --- manifest ------------------------------------------------------------
    skin_rows = max((len(c) for c in skin_columns), default=1)
    for position, model in enumerate(models):
        report.pev_body[model.name] = position + 1
        entry: dict[str, int] = {"pev_body": position + 1}
        if len(parse_texturegroups(model.qc_text)) > 1:
            entry["skins"] = skin_rows
        report.manifest[model.name] = entry
    if write_manifest:
        write_manifest_data(
            out_dir,
            {key: dict(value) for key, value in report.manifest.items()},
            manifest_format,
        )
    return report


__all__ = ["FLASH_BONE", "WEAPON_BONE", "merge_world_models"]
