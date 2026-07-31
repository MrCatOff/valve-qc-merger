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
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.merge_view.bodygroups import ModelParts
from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.merge_view.skeleton_ops import conform_to_table, fk_worlds
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import Smd, Triangle
from valve_qc_merger.transform import matrix_to_euler
from valve_qc_merger.writers.smd import write_smd_text

BONE_LIMIT = 127
BODYPART_LIMIT = 32
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
    warnings: list[str] = field(default_factory=list)


def _check_skeleton_consistency(models: list[ModelInput]) -> dict[str, str | None]:
    """One global name -> parent-name table; conflicting parentage is fatal."""
    merged: dict[str, str | None] = {}
    owner: dict[str, str] = {}
    for model in models:
        fullest = max(model.meshes.values(), key=lambda m: len(m.nodes))
        name_of = {n.index: n.name for n in fullest.nodes}
        for node in fullest.nodes:
            parent = name_of.get(node.parent) if node.parent >= 0 else None
            if node.name in merged and merged[node.name] != parent:
                raise MergeError(
                    f"bone {node.name!r} has parent {merged[node.name]!r} in "
                    f"{owner[node.name]} but {parent!r} in {model.name}; "
                    "pooling should have prevented this"
                )
            merged.setdefault(node.name, parent)
            owner.setdefault(node.name, model.name)
    return merged


def _unify_skeletons(models: list[ModelInput], skeleton: dict[str, str | None]) -> None:
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

    for model in models:
        for smd in {**model.meshes, **model.anims}.values():
            conform_to_table(smd, table, bind_locals)


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


def _content_hash(smd: Smd) -> str:
    return hashlib.md5(write_smd_text(smd).encode("latin-1")).hexdigest()[:10]


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
                    continue
                data = source.read_bytes()
                final = renames.get(material, material)
                if final in staged and staged[final] != data:
                    final = f"{model.name}__{material}"
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


def _unique_sequence_names(models: list[ModelInput]) -> dict[tuple[str, str], str]:
    taken: set[str] = set()
    out: dict[tuple[str, str], str] = {}
    for model in models:
        for seq_name in model.anims:
            final = seq_name if seq_name not in taken else f"{model.name}__{seq_name}"
            counter = 2
            while final in taken:
                final = f"{model.name}__{seq_name}_{counter}"
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
) -> MergeReport:
    """Write the merged model directory; returns the budget report."""
    report = MergeReport()
    models = [model for model, _ in pairs]
    skeleton = _check_skeleton_consistency(models)
    report.bones = len(skeleton)
    _unify_skeletons(models, skeleton)
    if report.bones > BONE_LIMIT:
        report.warnings.append(
            f"merged skeleton has {report.bones} bones (limit {BONE_LIMIT})"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "_shared").mkdir(exist_ok=True)

    # --- meshes -----------------------------------------------------------
    written: dict[str, list[Smd]] = {}
    weapon_paths: dict[str, list[str]] = {}  # model -> qc studio paths
    hands_paths: dict[str, str] = {}
    shared_hands: dict[str, str] = {}  # content hash -> qc path
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
            hands = model.meshes[parts.hands_stem]
            digest = _content_hash(hands)
            if digest not in shared_hands:
                shared_hands[digest] = f"_shared\\hands_{digest}"
                (out_dir / "_shared" / f"hands_{digest}.smd").write_text(
                    write_smd_text(hands), encoding="latin-1"
                )
            hands_paths[model.name] = shared_hands[digest]
            written[model.name].append(hands)

    _stage_textures(out_dir, models, written, report)
    # Re-write meshes whose materials were renamed by texture conflicts.
    for model, parts in pairs:
        model_dir = out_dir / model.name
        for index, _group in enumerate(parts.weapon_stems):
            stem = "weapon" if index == 0 else f"weapon_{index + 1}"
            (model_dir / f"{stem}.smd").write_text(
                write_smd_text(written[model.name][index]), encoding="latin-1"
            )

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
    hands_unique: list[str] = []
    for path in hands_paths.values():
        if path not in hands_unique:
            hands_unique.append(path)

    groups: list[tuple[str, list[str]]] = []
    groups.append(("weapon", [weapon_paths[m.name][0] for m in models]))
    for extra in range(1, max_weapon_groups):
        entries = ["blank"] + [
            weapon_paths[m.name][extra] for m in models
            if len(weapon_paths[m.name]) > extra
        ]
        groups.append((f"weapon_{extra + 1}", entries))
    if hands_unique:
        groups.append(("hands", hands_unique))
    report.bodyparts = len(groups)
    if report.bodyparts > BODYPART_LIMIT:
        report.warnings.append(
            f"{report.bodyparts} bodyparts (limit {BODYPART_LIMIT})"
        )
    if report.textures > TEXTURE_WARN:
        report.warnings.append(
            f"{report.textures} textures staged (studiomdl degrades past "
            f"~{TEXTURE_WARN}; part splitting arrives in M5)"
        )

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
            else:  # hands
                index = (
                    entries.index(hands_paths[model.name])
                    if model.name in hands_paths else 0
                )
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

    used_attachment_ids: set[int] = set()
    for model in models:
        for raw in model.qc_text.splitlines():
            stripped = raw.strip()
            if stripped.startswith("$attachment"):
                try:
                    attachment_id = int(stripped.split()[1])
                except (IndexError, ValueError):
                    continue
                if attachment_id not in used_attachment_ids and attachment_id <= 3:
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

    _write_manifest(out_dir, name, models, report, manifest_anim, manifest_format)
    return report


def _write_manifest(
    out_dir: Path, name: str, models: list[ModelInput], report: MergeReport,
    anim: dict[str, dict[str, int]], manifest_format: str,
) -> None:
    data = {
        model.name: {
            "pev_body": report.pev_body.get(model.name, 0),
            **{f"anim_{k}": v for k, v in anim[model.name].items()},
        }
        for model in models
    }
    if manifest_format == "json":
        (out_dir / "models.json").write_text(json.dumps(data, indent=1))
    elif manifest_format == "toml":
        chunks = []
        for section, values in data.items():
            chunks.append(f'["{section}"]')
            chunks.extend(f"{k} = {v}" for k, v in values.items())
            chunks.append("")
        (out_dir / "models.toml").write_text("\n".join(chunks))
    else:
        chunks = []
        for section, values in data.items():
            chunks.append(f"[{section}]")
            chunks.extend(f"{k} = {v}" for k, v in values.items())
            chunks.append("")
        (out_dir / "models.ini").write_text("\n".join(chunks))


__all__ = ["MergeError", "MergeReport", "merge_models"]
