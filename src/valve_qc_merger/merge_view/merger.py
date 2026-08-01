"""Model merging for merge-view (spec §3.8–3.9).

Combines canonicalised, pooled models into one output: per-model mesh SMDs
(weapon pieces concatenated per submodel, identical hand meshes shared by
content hash), all sequences, a fresh QC with ``$bodygroup`` blocks aligned so
one ``pev_body`` value selects a weapon's meshes together, staged textures
(md5-deduped; same-name-different-bytes renamed per model), and a per-model
manifest (ini/json/toml) with ``pev_body`` and merged sequence indices.
"""

from __future__ import annotations

import dataclasses
import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.merge_view.animsize import SEQ_DATA_LIMIT, sequence_sizes
from valve_qc_merger.merge_view.bodygroups import ModelParts
from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.merge_view.skeleton_ops import (
    conform_to_table,
    fk_worlds,
)
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import Smd, Triangle
from valve_qc_merger.transform import matrix_to_euler
from valve_qc_merger.writers.smd import write_smd_text

BONE_LIMIT = 127
BODYPART_LIMIT = 32
SUBMODEL_LIMIT = 32  # hard studiomdl array size; exceeding = memory corruption
STOCK_VERT_LIMIT = 2048  # stock MAXSTUDIOVERTS per submodel (verts and normals)
SUBMODEL_TRI_WARN = 4080  # old GoldSrc renderers degrade past this per submodel
TEXTURE_WARN = 80


class MergeError(RuntimeError):
    """A merge-level failure (inconsistent skeletons, budget violations)."""


@dataclass
class MergeReport:
    """What the merge produced, plus budget accounting."""

    bones: int = 0
    bodyparts: int = 0
    sequences: int = 0
    textures: int = 0
    pev_body: dict[str, int] = field(default_factory=dict)
    manifest: dict[str, dict[str, int]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def merged_skeleton(models: list[ModelInput]) -> dict[str, str | None]:
    """One global name -> parent-name table, first owner wins.

    Pooling may hand a later model a slot whose canonical parent that model
    never carried, so its SMDs still show the natural parent; the first
    owner's (= the pool's) parentage is canonical and ``unify_skeletons``
    re-solves every disagreeing SMD onto it exactly, per frame.
    """
    merged: dict[str, str | None] = {}
    for model in models:
        fullest = max(model.meshes.values(), key=lambda m: len(m.nodes))
        name_of = {n.index: n.name for n in fullest.nodes}
        for node in fullest.nodes:
            parent = name_of.get(node.parent) if node.parent >= 0 else None
            merged.setdefault(node.name, parent)
    return merged


def unify_skeletons(models: list[ModelInput], skeleton: dict[str, str | None]) -> None:
    """Graft the full merged skeleton into every SMD (prior-art _unify_skeleton).

    Every mesh and sequence SMD of every model ends up with the identical node
    table (names, parents, order). Missing bones are grafted statically at the
    bind-local transform taken from the first model that owns them — those are
    always another weapon's (hidden) bones, so a foreign bind is harmless,
    while studiomdl no longer invents defaults for absent sequence bones.
    """
    # Global topological order, deterministic: parents first, then name.
    order: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        parent = skeleton.get(name)
        if parent is not None:
            visit(parent)
        seen.add(name)
        order.append(name)

    for name in sorted(skeleton):
        visit(name)
    table = [(name, skeleton[name]) for name in order]

    # Bind locals from each bone's first owner (world -> parent-local).
    bind_locals: dict[str, tuple[Vector3, Vector3]] = {}
    for model in models:
        fullest = max(model.meshes.values(), key=lambda m: len(m.nodes))
        if not fullest.frames:
            continue
        worlds = fk_worlds(fullest, fullest.frames[0])
        name_of = {n.index: n.name for n in fullest.nodes}
        world_by_name = {name_of[i]: t for i, t in worlds.items()}
        for name, transform in world_by_name.items():
            if name in bind_locals:
                continue
            parent = skeleton.get(name)
            if parent is not None and parent in world_by_name:
                local = world_by_name[parent].inverse().compose(transform)
            else:
                local = transform
            bind_locals[name] = (local.translation, matrix_to_euler(local.rotation))
    zero = Vector3(0.0, 0.0, 0.0)
    for name, _parent in table:
        bind_locals.setdefault(name, (zero, zero))

    # Each mesh SMD KEEPS ITS OWN BIND for its native bones (only missing
    # bones are grafted at the first owner's bind): studiomdl recomputes
    # bonefixup from every reference file's own skeleton before localising
    # that file's vertices (Build_Reference at the top of Grab_Triangles), so
    # per-file binds are exactly what it expects — and every weapon.smd stays
    # byte-faithful to the decompiled original when inspected in Blender.
    # (Prior art: goldsource-models _unify_skeleton, which never moved verts.)
    for model in models:
        for smd in {**model.meshes, **model.anims}.values():
            conform_to_table(smd, table, bind_locals)


def _sequence_size_warnings(models: list[ModelInput], report: MergeReport) -> None:
    """Check every sequence against studiomdl's 64K anim-stream cap.

    Uses the byte-exact quantization+RLE replica (animsize module); with
    reparent-free pooling an overflow here means the SOURCE model's sequence
    is too big to recompile with stock studiomdl at all.
    """
    for (model_name, seq_name), size in sequence_sizes(models).items():
        if size > SEQ_DATA_LIMIT:
            report.warnings.append(
                f"{model_name}: sequence {seq_name!r} anim data is {size} "
                f"bytes (studiomdl caps one sequence's stream at 64K); "
                "studiomdl will refuse to compile this part"
            )


def _concat_meshes(meshes: list[Smd]) -> Smd:
    """One submodel SMD from several meshes sharing a node table."""
    first = meshes[0]
    table = [(n.index, n.name, n.parent) for n in first.nodes]
    triangles: list[Triangle] = []
    for mesh in meshes:
        if [(n.index, n.name, n.parent) for n in mesh.nodes] != table:
            raise MergeError("always-on meshes of one model disagree on the skeleton")
        triangles.extend(mesh.triangles)
    return Smd(nodes=list(first.nodes), frames=list(first.frames),
               triangles=triangles)


def _sanitize_material(material: str) -> str:
    """Delivery contract: printable ASCII, no spaces, .bmp extension.

    Spaces are outright fatal: studiomdl tokenizes SMD triangle headers on
    whitespace, so a material like ``king cobra scope.bmp`` crashes the
    compile mid-parse.
    """
    cleaned = "".join(ch if "!" <= ch <= "~" else "_" for ch in material)
    if not cleaned.lower().endswith(".bmp"):
        cleaned += ".bmp"
    return cleaned


def _find_texture(directory: Path, material: str) -> Path | None:
    wanted = {material.lower(), (material + ".bmp").lower()}
    for candidate in sorted(directory.iterdir()):
        if candidate.is_file() and candidate.name.lower() in wanted:
            return candidate
    return None


def _stage_textures(
    out_dir: Path, models: list[ModelInput],
    written: dict[str, list[Smd]], report: MergeReport,
) -> None:
    """Copy every referenced texture next to the QC, deduped by content."""
    staged: dict[str, bytes] = {}
    for model in models:
        renames: dict[str, str] = {}
        for smd in written[model.name]:
            for material in sorted({t.material for t in smd.triangles}):
                source = _find_texture(model.directory, material)
                if source is None:
                    report.warnings.append(
                        f"{model.name}: texture for material {material!r} not found"
                    )
                    sanitized = _sanitize_material(material)
                    if sanitized != material:
                        renames[material] = sanitized
                    continue
                data = source.read_bytes()
                final = renames.get(material, _sanitize_material(material))
                if final in staged and staged[final] != data:
                    final = _sanitize_material(f"{model.name}__{material}")
                if final != material:
                    renames[material] = final
                if final not in staged:
                    staged[final] = data
                    (out_dir / final).write_bytes(data)
        if renames:
            for smd in written[model.name]:
                smd.triangles = [
                    dataclasses.replace(t, material=renames.get(t.material, t.material))
                    for t in smd.triangles
                ]
    report.textures = len(staged)


SEQ_NAME_LIMIT = 31  # studiomdl strcpy's labels into char[32] unchecked


def _unique_sequence_names(models: list[ModelInput]) -> dict[tuple[str, str], str]:
    taken: set[str] = set()
    out: dict[tuple[str, str], str] = {}
    for model in models:
        for seq_name in model.anims:
            final = seq_name if seq_name not in taken else f"{model.name}__{seq_name}"
            final = final[:SEQ_NAME_LIMIT]
            counter = 2
            while final in taken:
                suffix = f"_{counter}"
                final = (f"{model.name}__{seq_name}"[:SEQ_NAME_LIMIT - len(suffix)]
                         + suffix)
                counter += 1
            taken.add(final)
            out[(model.name, seq_name)] = final
    return out


def merge_models(
    pairs: list[tuple[ModelInput, ModelParts]],
    out_dir: Path,
    name: str,
    *,
    manifest_format: str = "ini",
    write_manifest: bool = True,
) -> MergeReport:
    """Write the merged model directory; returns the budget report."""
    report = MergeReport()
    models = [model for model, _ in pairs]
    skeleton = merged_skeleton(models)
    report.bones = len(skeleton)
    unify_skeletons(models, skeleton)
    _sequence_size_warnings(models, report)
    if report.bones > BONE_LIMIT:
        report.warnings.append(
            f"merged skeleton has {report.bones} bones (limit {BONE_LIMIT})"
        )

    out_dir.mkdir(parents=True, exist_ok=True)

    # --- textures ---------------------------------------------------------
    # Sanitise material names and stage files FIRST, so the renames land in
    # every mesh written below.
    kept: dict[str, list[Smd]] = {}
    for model, parts in pairs:
        stems = [stem for group in parts.weapon_stems for stem in group]
        if parts.hands_stem is not None:
            stems.append(parts.hands_stem)
        kept[model.name] = [model.meshes[stem] for stem in stems]
    _stage_textures(out_dir, models, kept, report)

    # --- meshes -----------------------------------------------------------
    written: dict[str, list[Smd]] = {}
    weapon_paths: dict[str, list[str]] = {}  # model -> qc studio paths
    hands_paths: dict[str, str] = {}
    max_weapon_groups = 0
    for model, parts in pairs:
        model_dir = out_dir / model.name
        model_dir.mkdir(exist_ok=True)
        written[model.name] = []
        paths: list[str] = []
        for index, group in enumerate(parts.weapon_stems):
            merged_mesh = _concat_meshes([model.meshes[s] for s in group])
            stem = "weapon" if index == 0 else f"weapon_{index + 1}"
            written[model.name].append(merged_mesh)
            paths.append(f"{model.name}\\{stem}")
            (model_dir / f"{stem}.smd").write_text(
                write_smd_text(merged_mesh), encoding="latin-1"
            )
        weapon_paths[model.name] = paths
        max_weapon_groups = max(max_weapon_groups, len(paths))
        if parts.hands_stem is not None:
            # Hands live IN the model's folder (prior-art layout): every mesh
            # SMD keeps its model's own bind, so a hands file only pairs with
            # its own weapon's armature — a shared folder invites importing a
            # foreign-bind hands mesh, which shreds in Blender.
            hands = model.meshes[parts.hands_stem]
            (model_dir / "hands.smd").write_text(
                write_smd_text(hands), encoding="latin-1"
            )
            hands_paths[model.name] = f"{model.name}\\hands"
            written[model.name].append(hands)

    # --- animations -------------------------------------------------------
    seq_names = _unique_sequence_names(models)
    sequence_order: list[tuple[str, str, str]] = []  # (model, final name, path)
    for model in models:
        for seq_name, anim in model.anims.items():
            final = seq_names[(model.name, seq_name)]
            (out_dir / model.name / f"{final}.smd").write_text(
                write_smd_text(anim), encoding="latin-1"
            )
            sequence_order.append((model.name, final, f"{model.name}\\{final}"))
    report.sequences = len(sequence_order)

    # --- bodygroups + pev_body -------------------------------------------
    groups: list[tuple[str, list[str]]] = []
    groups.append(("weapon", [weapon_paths[m.name][0] for m in models]))
    for extra in range(1, max_weapon_groups):
        entries = ["blank"] + [
            weapon_paths[m.name][extra] for m in models
            if len(weapon_paths[m.name]) > extra
        ]
        groups.append((f"weapon_{extra + 1}", entries))
    if hands_paths:
        # One entry per model, aligned with the weapon group, so one pev_body
        # index pairs each weapon with its own hands (prior-art layout).
        groups.append(("hands", [
            hands_paths.get(m.name, "blank") for m in models
        ]))
    report.bodyparts = len(groups)
    if report.bodyparts > BODYPART_LIMIT:
        report.warnings.append(
            f"{report.bodyparts} bodyparts (limit {BODYPART_LIMIT})"
        )
    submodels = sum(len(entries) for _name, entries in groups)
    if submodels > SUBMODEL_LIMIT:
        raise MergeError(
            f"{submodels} submodels in one model exceed studiomdl's hard "
            f"{SUBMODEL_LIMIT}-entry arrays (silent memory corruption: "
            "compiled meshes detach from bones); split into more parts"
        )
    if report.textures > TEXTURE_WARN:
        report.warnings.append(
            f"{report.textures} textures staged (studiomdl degrades past "
            f"~{TEXTURE_WARN}; lower the texture budget to split further)"
        )
    overruns: set[str] = set()
    for model_name, smds in written.items():
        for smd in smds:
            verts = {(v.position, v.bone) for t in smd.triangles for v in t.vertices}
            norms = {(v.normal, v.bone) for t in smd.triangles for v in t.vertices}
            if len(verts) > STOCK_VERT_LIMIT or len(norms) > STOCK_VERT_LIMIT:
                overruns.add(
                    f"{model_name}: submodel has {len(verts)} verts / "
                    f"{len(norms)} normals (stock studiomdl caps both at "
                    f"{STOCK_VERT_LIMIT}; needs a raised-limit compiler)"
                )
            if len(smd.triangles) > SUBMODEL_TRI_WARN:
                overruns.add(
                    f"{model_name}: submodel has {len(smd.triangles)} tris "
                    f"(GoldSrc renderers degrade past ~{SUBMODEL_TRI_WARN} "
                    "per submodel)"
                )
    report.warnings.extend(sorted(overruns))

    for position, model in enumerate(models):
        value = 0
        stride = 1
        for group_name, entries in groups:
            if group_name == "weapon":
                index = position
            elif group_name.startswith("weapon_"):
                extra = int(group_name.split("_")[1]) - 1
                paths = weapon_paths[model.name]
                index = entries.index(paths[extra]) if len(paths) > extra else 0
            else:  # hands - aligned with the weapon group by construction
                index = position
            value += index * stride
            stride *= len(entries)
        report.pev_body[model.name] = value

    # --- QC ---------------------------------------------------------------
    lines: list[str] = [
        "// Generated by valve-qc-merger merge-view: canonical hands, pooled",
        "// weapon bones, one weapon per pev_body selection.",
        "",
        f'$modelname "{name}.mdl"',
        '$cd "."',
        '$cdtexture "."',
        "$cliptotextures",
        "$scale 1.0",
        "",
    ]
    for group_name, entries in groups:
        lines.append(f'$bodygroup "{group_name}"')
        lines.append("{")
        for entry in entries:
            lines.append("\tblank" if entry == "blank" else f'\tstudio "{entry}"')
        lines.append("}")
    lines.append("")
    lines.append("$flags 0")
    lines.append("")

    # studiomdl's compiled bone table keeps only vertex-used bones and their
    # ancestors; an attachment naming any other bone is a hard compile error.
    surviving: set[str] = set()
    for smds in written.values():
        for smd in smds:
            names = {n.index: n.name for n in smd.nodes}
            parents = {n.name: names.get(n.parent) for n in smd.nodes}
            for triangle in smd.triangles:
                for vertex in triangle.vertices:
                    bone: str | None = names[vertex.bone]
                    while bone is not None and bone not in surviving:
                        surviving.add(bone)
                        bone = parents.get(bone)
    used_attachment_ids: set[int] = set()
    for model in models:
        for raw in model.qc_text.splitlines():
            stripped = raw.strip()
            if stripped.startswith("$attachment"):
                try:
                    attachment_id = int(stripped.split()[1])
                    attachment_bone = shlex.split(stripped)[2]
                except (IndexError, ValueError):
                    continue
                if attachment_id in used_attachment_ids or attachment_id > 3:
                    continue
                if attachment_bone not in surviving:
                    report.warnings.append(
                        f"{model.name}: attachment {attachment_id} dropped - "
                        f"bone {attachment_bone!r} carries no vertices and is "
                        "not in the compiled bone table"
                    )
                    continue
                used_attachment_ids.add(attachment_id)
                lines.append(stripped)
    lines.append("")

    manifest_anim: dict[str, dict[str, int]] = {m.name: {} for m in models}
    for index, (model_name, final, smd_path) in enumerate(sequence_order):
        model = next(m for m in models if m.name == model_name)
        original = next(
            orig for (mname, orig), fin in seq_names.items()
            if mname == model_name and fin == final
        )
        seq_meta = next((s for s in model.sequences if s.name == original), None)
        lines.append(f'$sequence "{final}" {{')
        lines.append(f'\t"{smd_path}"')
        if seq_meta is not None:
            for event in seq_meta.events:
                lines.append(f"\t{event}")
            if seq_meta.fps is not None:
                fps = int(seq_meta.fps) if seq_meta.fps == int(seq_meta.fps) else seq_meta.fps
                lines.append(f"\tfps {fps}")
        lines.append("}")
        manifest_anim[model_name][original] = index
    (out_dir / f"{name}.qc").write_text("\n".join(lines) + "\n", encoding="latin-1")

    report.manifest = {
        model.name: {
            "pev_body": report.pev_body.get(model.name, 0),
            **{f"anim_{k}": v for k, v in manifest_anim[model.name].items()},
        }
        for model in models
    }
    if write_manifest:
        write_manifest_data(out_dir, report.manifest, manifest_format)
    return report


def write_manifest_data(
    out_dir: Path, data: dict[str, dict[str, object]] | dict[str, dict[str, int]],
    manifest_format: str,
) -> None:
    """Write the models manifest in the chosen format."""
    if manifest_format == "json":
        (out_dir / "models.json").write_text(json.dumps(data, indent=1))
    elif manifest_format == "toml":
        chunks = []
        for section, values in data.items():
            chunks.append(f'["{section}"]')
            chunks.extend(
                f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {v}"
                for k, v in values.items()
            )
            chunks.append("")
        (out_dir / "models.toml").write_text("\n".join(chunks))
    else:
        chunks = []
        for section, values in data.items():
            chunks.append(f"[{section}]")
            chunks.extend(f"{k} = {v}" for k, v in values.items())
            chunks.append("")
        (out_dir / "models.ini").write_text("\n".join(chunks))


__all__ = ["MergeError", "MergeReport", "merge_models", "write_manifest_data"]
