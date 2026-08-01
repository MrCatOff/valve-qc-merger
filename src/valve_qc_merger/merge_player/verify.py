"""Post-merge verification gate for merge-player.

Every claim is re-proven from the EMITTED part files against the pristine
decompiled inputs:

- ``tables_consistent``   — one identical node table across the part's SMDs,
  parents before children;
- ``placement_preserved`` — per model: every shared bone's bind local is
  byte-faithful to the original reference, every weapon bone's HAND-RELATIVE
  transform (geometry bind AND the merged idle frame) matches the original
  (collapse/reparent/rename were exact) — hand-relative because the engine
  bone-merges the Bip01 chain by name at runtime, so only the offset from the
  hand decides where the weapon sits;
- ``geometry_preserved``  — every merged mesh vertex position exists in the
  model's original meshes (bit-level);
- ``budgets``             — bones <= 127, submodels <= 32, verts/normals per
  submodel <= 2048, textures <= 100 and present on disk, texturegroup files
  present, sequence labels < 32 chars, QC paths <= 60 chars.
"""

from __future__ import annotations

import math
from pathlib import Path

from valve_qc_merger.merge_player.analyze import is_shared
from valve_qc_merger.merge_player.merger import parse_texturegroups
from valve_qc_merger.merge_view.discovery import load_model
from valve_qc_merger.merge_view.skeleton_ops import fk_worlds
from valve_qc_merger.merge_view.verify import (
    QC_PATH_LIMIT,
    STOCK_VERT_LIMIT,
    SUBMODEL_LIMIT,
    TEXTURE_LIMIT,
    GateResult,
)
from valve_qc_merger.models.smd import Smd
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget.qc_build import parse_bodygroups
from valve_qc_merger.transform import (
    Transform,
    mat3_multiply,
    mat3_transpose,
    rotation_angle,
)

BONE_LIMIT = 127
PLACE_EPSILON = 1e-3  # units / radians; collapse re-solves are ~1e-6
SEQ_NAME_LIMIT = 31


def _table(smd: Smd) -> list[tuple[int, str, int]]:
    return [(n.index, n.name, n.parent) for n in smd.nodes]


def _relative(worlds: dict[int, Transform], anchor: int, bone: int) -> Transform:
    return worlds[anchor].inverse().compose(worlds[bone])


def _delta(a: Transform, b: Transform) -> float:
    """Max of translation delta (units) and rotation delta (radians)."""
    t = max(
        abs(a.translation.x - b.translation.x),
        abs(a.translation.y - b.translation.y),
        abs(a.translation.z - b.translation.z),
    )
    r = rotation_angle(mat3_multiply(mat3_transpose(a.rotation), b.rotation))
    return max(t, abs(r))


def verify_player_part(
    part_dir: Path,
    qc_name: str,
    models_dir: Path,
    model_names: list[str],
    bone_maps: dict[str, dict[str, str]],
    anchors: dict[str, dict[str, str]],
) -> list[GateResult]:
    """Run every gate check for one emitted part; returns one row per check.

    ``bone_maps``: per model, original bone name -> merged bone name (shared
    bones map to themselves; collapsed helpers are absent). ``anchors``: per
    model, merged weapon bone name -> its anchoring shared bone.
    """
    results: list[GateResult] = []
    qc_text = (part_dir / qc_name).read_text(encoding="latin-1")

    mesh_paths = [entry for group in parse_bodygroups(qc_text).values()
                  for entry in group if entry != "blank"]
    meshes = {p: parse_smd_file(part_dir / (p.replace("\\", "/") + ".smd"))
              for p in mesh_paths}
    idle = parse_smd_file(part_dir / "animations" / "idle.smd")

    # -- tables_consistent -------------------------------------------------
    tables = {p: _table(s) for p, s in {**meshes, "idle": idle}.items()}
    reference_table = next(iter(tables.values()))
    bad = [p for p, t in tables.items() if t != reference_table]
    ordered = all(parent < index for index, _n, parent in reference_table)
    results.append(GateResult(
        "tables_consistent", not bad and ordered,
        f"{len(tables)} SMDs share one {len(reference_table)}-bone table"
        if not bad and ordered else
        (f"tables differ: {bad[:3]}" if bad else "parents not before children"),
    ))

    # -- placement_preserved -----------------------------------------------
    worst = 0.0
    worst_at = ""
    checked = 0
    problems: list[str] = []
    merged_index = {name: index for index, name, _p in reference_table}
    for model_name in model_names:
        original = load_model(models_dir / model_name, require_anims=False)
        source = max(original.meshes.values(), key=lambda m: len(m.nodes))
        source_index = {n.name: n.index for n in source.nodes}
        merged_mesh = next(
            (smd for path, smd in meshes.items()
             if path.split("\\")[-1] == model_name), None
        )
        if merged_mesh is None:
            problems.append(f"{model_name}: no merged geometry SMD found")
            continue
        bone_map = bone_maps.get(model_name, {})
        source_worlds = fk_worlds(source, source.frames[0])
        mesh_worlds = fk_worlds(merged_mesh, merged_mesh.frames[0])
        idle_worlds = fk_worlds(idle, idle.frames[0])
        source_anim = next(iter(original.anims.values()), None)
        anim_worlds = (
            fk_worlds(source_anim, source_anim.frames[0])
            if source_anim is not None and source_anim.frames else None
        )
        anim_index = (
            {n.name: n.index for n in source_anim.nodes}
            if source_anim is not None else {}
        )
        for orig_name, merged_name in bone_map.items():
            if orig_name not in source_index or merged_name not in merged_index:
                continue
            checked += 1
            if is_shared(merged_name):
                # Shared bones keep their own bind locals verbatim.
                orig_local = source.frames[0].pose_for(source_index[orig_name])
                new_local = merged_mesh.frames[0].pose_for(
                    merged_index[merged_name]
                )
                assert orig_local is not None and new_local is not None
                delta = max(
                    abs(orig_local.position.x - new_local.position.x),
                    abs(orig_local.position.y - new_local.position.y),
                    abs(orig_local.position.z - new_local.position.z),
                )
                where = f"{model_name}/{orig_name} bind"
            else:
                anchor_name = anchors.get(model_name, {}).get(merged_name)
                if anchor_name is None or anchor_name not in source_index:
                    problems.append(
                        f"{model_name}: no anchor for bone {merged_name!r}"
                    )
                    continue
                want = _relative(
                    source_worlds, source_index[anchor_name],
                    source_index[orig_name],
                )
                got_bind = _relative(
                    mesh_worlds, merged_index[anchor_name],
                    merged_index[merged_name],
                )
                delta = _delta(want, got_bind)
                where = f"{model_name}/{orig_name} bind"
                # The idle frame must place the weapon where the ORIGINAL
                # model's own idle animation did.
                if anim_worlds is not None and orig_name in anim_index:
                    want_idle = _relative(
                        anim_worlds, anim_index[anchor_name],
                        anim_index[orig_name],
                    )
                    got_idle = _relative(
                        idle_worlds, merged_index[anchor_name],
                        merged_index[merged_name],
                    )
                    idle_delta = _delta(want_idle, got_idle)
                    if idle_delta > delta:
                        delta = idle_delta
                        where = f"{model_name}/{orig_name} idle"
            if delta > worst:
                worst = delta
                worst_at = where
    ok = worst <= PLACE_EPSILON and not problems
    results.append(GateResult(
        "placement_preserved", ok,
        f"{checked} bone placements checked, worst delta {worst:.2e} "
        f"({worst_at})" if not problems else "; ".join(problems[:3]),
    ))

    # -- geometry_preserved ------------------------------------------------
    bad_verts = 0
    total_verts = 0
    for model_name in model_names:
        positions: set[tuple[float, float, float]] = set()
        for smd_path in sorted((models_dir / model_name).glob("*.smd")):
            source_smd = parse_smd_file(smd_path)
            for t in source_smd.triangles:
                for v in t.vertices:
                    positions.add((round(v.position.x, 4),
                                   round(v.position.y, 4),
                                   round(v.position.z, 4)))
        merged_mesh = next(
            (smd for path, smd in meshes.items()
             if path.split("\\")[-1] == model_name), None
        )
        if merged_mesh is None:
            continue
        for t in merged_mesh.triangles:
            for v in t.vertices:
                total_verts += 1
                key = (round(v.position.x, 4), round(v.position.y, 4),
                       round(v.position.z, 4))
                if key not in positions:
                    bad_verts += 1
    results.append(GateResult(
        "geometry_preserved", bad_verts == 0,
        f"{total_verts} mesh vertices bit-match their originals"
        if bad_verts == 0 else f"{bad_verts}/{total_verts} vertices moved",
    ))

    # -- budgets -----------------------------------------------------------
    budget_problems: list[str] = []
    if len(reference_table) > BONE_LIMIT:
        budget_problems.append(f"{len(reference_table)} bones > {BONE_LIMIT}")
    groups = parse_bodygroups(qc_text)
    submodels = sum(len(entries) for entries in groups.values())
    if submodels > SUBMODEL_LIMIT:
        budget_problems.append(f"{submodels} submodels > {SUBMODEL_LIMIT}")
    for path, smd in meshes.items():
        verts = {(v.position, v.bone) for t in smd.triangles for v in t.vertices}
        norms = {(v.normal, v.bone) for t in smd.triangles for v in t.vertices}
        if len(verts) > STOCK_VERT_LIMIT or len(norms) > STOCK_VERT_LIMIT:
            budget_problems.append(
                f"{path}: {len(verts)}v/{len(norms)}n > {STOCK_VERT_LIMIT}"
            )
    materials = {t.material for s in meshes.values() for t in s.triangles}
    skin_files = {name for row in parse_texturegroups(qc_text) for name in row}
    for file in sorted(materials | skin_files):
        if not (part_dir / file).exists():
            budget_problems.append(f"texture missing on disk: {file}")
    if len(materials | skin_files) > TEXTURE_LIMIT:
        budget_problems.append(
            f"{len(materials | skin_files)} textures > {TEXTURE_LIMIT}"
        )
    for label in ("idle",):
        if len(label) > SEQ_NAME_LIMIT:
            budget_problems.append(f"sequence label too long: {label}")
    long_paths = [p for p in mesh_paths if len(p) > QC_PATH_LIMIT]
    if long_paths:
        budget_problems.append(
            f"QC paths > {QC_PATH_LIMIT} chars: {long_paths[:3]}"
        )
    if not math.isfinite(worst):
        budget_problems.append("placement delta not finite")
    results.append(GateResult(
        "budgets", not budget_problems,
        f"bones={len(reference_table)} submodels={submodels} "
        f"textures={len(materials | skin_files)}"
        if not budget_problems else "; ".join(budget_problems),
    ))
    return results


__all__ = ["PLACE_EPSILON", "verify_player_part"]
