"""Model merging for merge-player.

Emits one compilable part: per-weapon geometry SMDs (each keeping its own
bind, on one identical node table), a single-frame ``idle`` sequence carrying
every weapon bone's own hand-relative local transform, staged textures with a
column-merged ``$texturegroup`` (CSO upgrade skins keep working: only one
weapon is visible at a time, so a global skin row is harmless), and a QC whose
single ``weapons`` bodygroup starts with ``blank`` — ``pev_body 0`` shows
nothing, weapon *i* is ``pev_body i+1``.

Per-weapon ``$attachment`` entries are dropped: GoldSource caps a model at 4
attachments, so per-weapon muzzle-flash points cannot survive a 30-weapon
merge (the classic community weapons.mdl ships the same way).
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from valve_qc_merger.merge_player.analyze import PlayerPlan
from valve_qc_merger.merge_view.atlas import (
    TextureOptions,
    downscale_textures,
    pack_textures,
)
from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.merge_view.merger import (
    BONE_LIMIT,
    STOCK_VERT_LIMIT,
    SUBMODEL_LIMIT,
    SUBMODEL_TRI_WARN,
    TEXTURE_WARN,
    MergeError,
    MergeReport,
    _collect_render_modes,
    _concat_meshes,
    _find_texture,
    _sanitize_material,
    _stage_textures,
    merged_skeleton,
    unify_skeletons,
    write_manifest_data,
)
from valve_qc_merger.models.smd import BonePose, Frame, Smd
from valve_qc_merger.writers.smd import write_smd_text

_TEXGROUP_RE = re.compile(r"\$texturegroup[^{]*\{(.*?)\n\s*\}", re.DOTALL)
_TEXGROUP_ROW_RE = re.compile(r"\{([^{}]*)\}")


def parse_texturegroups(qc_text: str) -> list[list[str]]:
    """Skin-family rows from a QC's ``$texturegroup`` block (may be empty)."""
    match = _TEXGROUP_RE.search(qc_text)
    if match is None:
        return []
    rows = [
        re.findall(r'"([^"]+)"', row)
        for row in _TEXGROUP_ROW_RE.findall(match.group(1))
    ]
    return [row for row in rows if row]


def skin_texture_files(qc_text: str) -> set[str]:
    """Texture files a QC's skin rows pull in beyond row 0 (the mesh set)."""
    rows = parse_texturegroups(qc_text)
    return {name for row in rows[1:] for name in row}


def _stage_skin_variants(
    out_dir: Path,
    models: list[ModelInput],
    staged_renames: dict[str, dict[str, str]],
    staged_names: list[str],
    report: MergeReport,
) -> tuple[list[list[str]], list[str], dict[str, list[list[str]]]]:
    """Stage skin-row textures and build merged ``$texturegroup`` columns.

    Returns ``(columns, variant_names, model_columns)``: each column is the
    staged file names of one original column, row 0 first; ``variant_names``
    are the extra files staged here (rows past 0); ``model_columns`` maps a
    model name to ITS columns, so the manifest can spell out which texture
    each pev_skin row shows for that weapon.
    """
    on_disk = {name.lower(): name for name in staged_names}
    columns: list[list[str]] = []
    variants: list[str] = []
    model_columns: dict[str, list[list[str]]] = {}
    for model in models:
        rows = parse_texturegroups(model.qc_text)
        if len(rows) < 2:
            continue
        if len({len(row) for row in rows}) != 1:
            report.warnings.append(
                f"{model.name}: $texturegroup rows have uneven lengths; "
                "skin families dropped"
            )
            continue
        renames = {k.lower(): v for k, v in staged_renames.get(model.name, {}).items()}
        for column in zip(*rows, strict=True):
            base = column[0]
            staged_base = renames.get(base.lower(), base)
            staged_base = on_disk.get(staged_base.lower())
            if staged_base is None:
                report.warnings.append(
                    f"{model.name}: skin family base texture {base!r} is not "
                    "used by any kept mesh; column dropped"
                )
                continue
            merged_column = [staged_base]
            for variant in column[1:]:
                source = _find_texture(model.directory, variant)
                if source is None:
                    report.warnings.append(
                        f"{model.name}: skin variant texture {variant!r} not found"
                    )
                    break
                data = source.read_bytes()
                final = _sanitize_material(variant)
                existing = out_dir / final
                if existing.exists() and existing.read_bytes() != data:
                    final = _sanitize_material(f"{model.name}__{variant}")
                if not (out_dir / final).exists():
                    (out_dir / final).write_bytes(data)
                    variants.append(final)
                    on_disk[final.lower()] = final
                merged_column.append(final)
            else:
                columns.append(merged_column)
                model_columns.setdefault(model.name, []).append(merged_column)
    return columns, variants, model_columns


def merge_player_models(
    pairs: list[tuple[ModelInput, PlayerPlan]],
    out_dir: Path,
    name: str,
    *,
    manifest_format: str = "ini",
    write_manifest: bool = True,
    textures: TextureOptions | None = None,
) -> MergeReport:
    """Write one merged p_ model part; returns the budget report."""
    report = MergeReport()
    models = [model for model, _ in pairs]
    skeleton = merged_skeleton(models)
    report.bones = len(skeleton)
    if report.bones > BONE_LIMIT:
        report.warnings.append(
            f"merged skeleton has {report.bones} bones (limit {BONE_LIMIT})"
        )
    unify_skeletons(models, skeleton)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "geometry").mkdir(exist_ok=True)
    (out_dir / "animations").mkdir(exist_ok=True)

    # --- per-weapon geometry (empty decompiler submodels dropped) ----------
    kept: dict[str, list[Smd]] = {}
    for model, _plan in pairs:
        non_empty = [smd for smd in model.meshes.values() if smd.triangles]
        if not non_empty:
            raise MergeError(f"model {model.name!r}: every mesh SMD is empty")
        if len(non_empty) < len(model.meshes):
            report.warnings.append(
                f"{model.name}: dropped "
                f"{len(model.meshes) - len(non_empty)} empty mesh SMDs"
            )
        kept[model.name] = [_concat_meshes(non_empty)]

    # --- textures ----------------------------------------------------------
    staged_renames, staged_names = _stage_textures(out_dir, models, kept, report)
    render_modes = _collect_render_modes(
        models, kept, staged_renames, staged_names, report,
    )
    skin_columns, variant_names, model_columns = _stage_skin_variants(
        out_dir, models, staged_renames, staged_names, report,
    )
    all_staged = staged_names + variant_names
    report.textures = len(all_staged)
    if textures is not None and textures.max_size is not None:
        downscale_textures(out_dir, all_staged, textures.max_size,
                           report.warnings)
    if textures is not None and textures.pack:
        # Skin-swapped textures must stay standalone files: a skin row swaps
        # whole textures, which an atlas tile cannot express.
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

    # --- geometry files ----------------------------------------------------
    report.bodyparts = 1
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
        verts = {(v.position, v.bone) for t in mesh.triangles for v in t.vertices}
        norms = {(v.normal, v.bone) for t in mesh.triangles for v in t.vertices}
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

    # --- the single-frame idle sequence ------------------------------------
    # After unify_skeletons every SMD shares one node table, and each weapon
    # bone's grafted local IS its own model's hand-relative bind. Start from
    # the first model's bind frame and override each weapon bone from its own
    # model's idle animation (frame 0) where one exists — that is the exact
    # pose the weapon showed in game.
    base = kept[models[0].name][0]
    idle_pose = {p.bone: p for p in base.frames[0].poses}
    index_of = {n.name: n.index for n in base.nodes}
    for model, plan in pairs:
        anim = next(iter(model.anims.values()), None)
        if anim is None or not anim.frames:
            continue
        anim_pose = {p.bone: p for p in anim.frames[0].poses}
        for bone in plan.bones:
            index = index_of[bone.final]
            idle_pose[index] = anim_pose[index]
    idle = Smd(
        nodes=list(base.nodes),
        frames=[Frame(0, tuple(
            BonePose(i, idle_pose[i].position, idle_pose[i].rotation)
            for i in sorted(idle_pose)
        ))],
        triangles=[],
    )
    (out_dir / "animations" / "idle.smd").write_text(
        write_smd_text(idle), encoding="latin-1"
    )
    report.sequences = 1

    # --- QC -----------------------------------------------------------------
    lines: list[str] = [
        "// Generated by valve-qc-merger merge-player: shared Bip01 chain,",
        "// one bone per held object, one weapon per pev_body selection",
        "// (pev_body 0 = blank).",
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
                "at 4 attachments; per-weapon flash points cannot merge)"
            )

    # --- manifest -----------------------------------------------------------
    skin_rows = max((len(c) for c in skin_columns), default=1)
    for position, (model, _plan) in enumerate(pairs):
        report.pev_body[model.name] = position + 1
        entry: dict[str, int | str] = {"pev_body": position + 1}
        own_columns = model_columns.get(model.name)
        if own_columns:
            # Spell out what each pev_skin row shows for THIS weapon, so an
            # amxx plugin can identify and switch skin variants by index.
            entry["skins"] = skin_rows
            for row in range(skin_rows):
                entry[f"skin_{row}"] = ",".join(
                    column[min(row, len(column) - 1)] for column in own_columns
                )
        report.manifest[model.name] = entry
    if write_manifest:
        write_manifest_data(
            out_dir,
            {key: dict(value) for key, value in report.manifest.items()},
            manifest_format,
        )
    return report


__all__ = [
    "merge_player_models",
    "parse_texturegroups",
    "skin_texture_files",
]
