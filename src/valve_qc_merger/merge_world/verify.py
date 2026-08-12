"""Post-merge verification gate for merge-w.

Every claim is re-proven from the EMITTED part files against the pristine
decompiled inputs:

- ``tables_consistent`` — every part SMD carries the identical two-bone
  ``flash -> weapon`` table with identity transforms;
- ``render_preserved``  — per model: every merged vertex sits exactly where
  the ORIGINAL model rendered it on screen (``idle_world . bind_world⁻¹``
  applied to the pristine vertices — identity where idle equals bind, so
  those vertices must be bit-identical);
- ``budgets``           — submodels <= 32, verts/normals per submodel
  <= 2048, textures <= 100 and present on disk (skin rows included), QC
  paths <= 60 chars.
"""

from __future__ import annotations

from pathlib import Path

from valve_qc_merger.commands.merge_player import _load_player_model
from valve_qc_merger.merge_player.merger import parse_texturegroups
from valve_qc_merger.merge_view.skeleton_ops import fk_worlds
from valve_qc_merger.merge_view.verify import (
    QC_PATH_LIMIT,
    STOCK_VERT_LIMIT,
    SUBMODEL_LIMIT,
    TEXTURE_LIMIT,
    GateResult,
)
from valve_qc_merger.merge_world.merger import FLASH_BONE, WEAPON_BONE
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget.qc_build import parse_bodygroups
from valve_qc_merger.transform import Transform

_ZERO = Vector3(0.0, 0.0, 0.0)


def verify_world_part(
    part_dir: Path,
    qc_name: str,
    models_dir: Path,
    model_names: list[str],
) -> list[GateResult]:
    """Run every gate check for one emitted part; returns one row per check."""
    results: list[GateResult] = []
    qc_text = (part_dir / qc_name).read_text(encoding="latin-1")

    mesh_paths = [entry for group in parse_bodygroups(qc_text).values()
                  for entry in group if entry != "blank"]
    meshes = {p: parse_smd_file(part_dir / (p.replace("\\", "/") + ".smd"))
              for p in mesh_paths}
    idle = parse_smd_file(part_dir / "animations" / "idle.smd")

    # -- tables_consistent -------------------------------------------------
    wanted_table = [(0, FLASH_BONE, -1), (1, WEAPON_BONE, 0)]
    bad = [
        p for p, smd in {**meshes, "idle": idle}.items()
        if [(n.index, n.name, n.parent) for n in smd.nodes] != wanted_table
        or any(
            pose.position != _ZERO or pose.rotation != _ZERO
            for frame in smd.frames for pose in frame.poses
        )
    ]
    results.append(GateResult(
        "tables_consistent", not bad,
        f"{len(meshes) + 1} SMDs share the identity "
        f"{FLASH_BONE}->{WEAPON_BONE} table"
        if not bad else f"tables differ from canonical: {bad[:3]}",
    ))

    # -- render_preserved ----------------------------------------------------
    bad_verts = 0
    total_verts = 0
    worst_model = ""
    for model_name in model_names:
        original = _load_player_model(models_dir / model_name)
        anim = next(iter(original.anims.values()), None)
        anim_worlds: dict[str, Transform] = {}
        if anim is not None and anim.frames:
            worlds = fk_worlds(anim, anim.frames[0])
            anim_worlds = {n.name: worlds[n.index] for n in anim.nodes}
        # Quantise exactly like the SMD writer (%.6f), so the expected
        # rendered position and the parsed emitted position are bit-equal.
        rendered: set[tuple[float, float, float]] = set()
        for source in original.meshes.values():
            if not source.frames:
                continue
            bind_worlds = fk_worlds(source, source.frames[0])
            transforms = {
                n.index: (
                    anim_worlds[n.name].compose(bind_worlds[n.index].inverse())
                    if n.name in anim_worlds else Transform.identity()
                )
                for n in source.nodes
            }
            for t in source.triangles:
                for v in t.vertices:
                    p = transforms[v.bone].transform_point(v.position)
                    rendered.add((float(f"{p.x:.6f}"), float(f"{p.y:.6f}"),
                                  float(f"{p.z:.6f}")))
        merged_mesh = next(
            (smd for path, smd in meshes.items()
             if path.split("\\")[-1] == model_name), None
        )
        if merged_mesh is None:
            bad_verts += 1
            worst_model = f"{model_name}: no merged geometry SMD"
            continue
        for t in merged_mesh.triangles:
            for v in t.vertices:
                total_verts += 1
                key = (float(f"{v.position.x:.6f}"),
                       float(f"{v.position.y:.6f}"),
                       float(f"{v.position.z:.6f}"))
                if key not in rendered:
                    bad_verts += 1
                    worst_model = model_name
    results.append(GateResult(
        "render_preserved", bad_verts == 0,
        f"{total_verts} mesh vertices match their original on-screen positions"
        if bad_verts == 0 else
        f"{bad_verts}/{total_verts} vertices misplaced ({worst_model})",
    ))

    # -- budgets --------------------------------------------------------------
    problems: list[str] = []
    groups = parse_bodygroups(qc_text)
    submodels = sum(len(entries) for entries in groups.values())
    if submodels > SUBMODEL_LIMIT:
        problems.append(f"{submodels} submodels > {SUBMODEL_LIMIT}")
    for path, smd in meshes.items():
        verts = {v.position for t in smd.triangles for v in t.vertices}
        norms = {v.normal for t in smd.triangles for v in t.vertices}
        if len(verts) > STOCK_VERT_LIMIT or len(norms) > STOCK_VERT_LIMIT:
            problems.append(
                f"{path}: {len(verts)}v/{len(norms)}n > {STOCK_VERT_LIMIT}"
            )
    materials = {t.material for s in meshes.values() for t in s.triangles}
    skin_files = {name for row in parse_texturegroups(qc_text) for name in row}
    for file in sorted(materials | skin_files):
        if not (part_dir / file).exists():
            problems.append(f"texture missing on disk: {file}")
    if len(materials | skin_files) > TEXTURE_LIMIT:
        problems.append(
            f"{len(materials | skin_files)} textures > {TEXTURE_LIMIT}"
        )
    long_paths = [p for p in mesh_paths if len(p) > QC_PATH_LIMIT]
    if long_paths:
        problems.append(f"QC paths > {QC_PATH_LIMIT} chars: {long_paths[:3]}")
    results.append(GateResult(
        "budgets", not problems,
        f"bones=2 submodels={submodels} "
        f"textures={len(materials | skin_files)}"
        if not problems else "; ".join(problems),
    ))
    return results


__all__ = ["verify_world_part"]
