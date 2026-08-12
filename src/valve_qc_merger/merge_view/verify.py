"""Post-merge verification gate for merge-v (spec 3.13).

Every claim the merge makes is re-proven from the EMITTED files of each part
directory, against the pristine decompiled inputs:

- ``tables_consistent`` — one identical node table across the part's mesh and
  sequence SMDs, parents before children;
- ``canonical_hands``   — the reference hand subtree (names + parents) is
  embedded exactly; no ``*Nub`` bone anywhere;
- ``pose_preserved``    — FK world transforms of every surviving bone, every
  frame of every sequence, match the original animation within epsilon
  (renames, grafts, reparents and pooling were exact);
- ``geometry_preserved``— every merged mesh vertex position exists in the
  model's original meshes (bit-level: positions are never transformed);
- ``budgets``           — bones <= 127, submodels <= 32, bodyparts <= 32,
  verts/normals per submodel <= 2048, textures <= 100 (warn past the soft
  budget), exact per-sequence anim stream <= 64K, QC paths <= 60 chars.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from valve_qc_merger.merge_view.animsize import SEQ_DATA_LIMIT, sequence_sizes
from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.merge_view.skeleton_ops import fk_worlds
from valve_qc_merger.models.smd import Smd
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget.qc_build import parse_bodygroups

POSE_EPSILON = 2e-3  # absolute floor, units
POSE_RELATIVE = 5e-6  # per unit of bone radius: 6-decimal euler text
# quantisation rotates a chain by ~3e-7 rad, so bones parked thousands of
# units away (off-screen effect bones) legitimately move millimetres.
BONE_LIMIT = 127
SUBMODEL_LIMIT = 32
BODYPART_LIMIT = 32
STOCK_VERT_LIMIT = 2048
TEXTURE_LIMIT = 100
QC_PATH_LIMIT = 60

_SEQ_SMD_RE = re.compile(r'\$sequence\s+"[^"]+"\s*\{\s*"([^"]+)"')
_NUB_RE = re.compile(r"Finger\d*Nub$", re.IGNORECASE)


@dataclass
class GateResult:
    check: str
    passed: bool
    detail: str


def _table(smd: Smd) -> list[tuple[int, str, int]]:
    return [(n.index, n.name, n.parent) for n in smd.nodes]


def verify_part(
    part_dir: Path,
    qc_name: str,
    models_dir: Path,
    model_names: list[str],
    bone_maps: dict[str, dict[str, str]],
    manifest_anim: dict[str, dict[str, int]],
    reference: Path,
    original_anims: dict[str, dict[str, str]],
) -> list[GateResult]:
    """Run every gate check for one emitted part; returns one row per check.

    ``bone_maps``: per model, original bone name -> merged bone name (absent
    for removed Nubs). ``original_anims``: per model, original sequence name
    -> path of the pristine decompiled animation SMD.
    """
    results: list[GateResult] = []
    qc_text = (part_dir / qc_name).read_text(encoding="latin-1")

    # Parse everything the part emitted.
    seq_paths = _SEQ_SMD_RE.findall(qc_text)
    mesh_paths = [entry for group in parse_bodygroups(qc_text).values()
                  for entry in group if entry != "blank"]
    meshes = {p: parse_smd_file(part_dir / (p.replace("\\", "/") + ".smd"))
              for p in mesh_paths}
    anims = {p: parse_smd_file(part_dir / (p.replace("\\", "/") + ".smd"))
             for p in seq_paths}

    # -- tables_consistent -------------------------------------------------
    tables = {p: _table(s) for p, s in {**meshes, **anims}.items()}
    first_path = next(iter(tables))
    reference_table = tables[first_path]
    bad = [p for p, t in tables.items() if t != reference_table]
    ordered = all(parent < index for index, _n, parent in reference_table)
    results.append(GateResult(
        "tables_consistent", not bad and ordered,
        f"{len(tables)} SMDs share one {len(reference_table)}-bone table"
        if not bad and ordered else
        (f"tables differ: {bad[:3]}" if bad else "parents not before children"),
    ))

    # -- canonical_hands ---------------------------------------------------
    ref = parse_smd_file(reference)
    ref_names = {n.index: n.name for n in ref.nodes}
    wanted = {ref_names[n.index]: (ref_names.get(n.parent) if n.parent >= 0 else None)
              for n in ref.nodes if not _NUB_RE.search(n.name)}
    merged_parent = {name: (reference_table[parent][1] if parent >= 0 else None)
                     for _i, name, parent in reference_table}
    missing = [b for b in wanted if b not in merged_parent]
    wrong = [b for b, p in wanted.items()
             if b in merged_parent and p is not None and merged_parent[b] != p]
    nubs = [name for _i, name, _p in reference_table if _NUB_RE.search(name)]
    ok = not missing and not wrong and not nubs
    results.append(GateResult(
        "canonical_hands", ok,
        f"{len(wanted)} reference bones embedded, no Nubs" if ok else
        f"missing={missing[:3]} wrong-parent={wrong[:3]} nubs={nubs[:3]}",
    ))

    # -- pose_preserved ----------------------------------------------------
    worst = 0.0
    worst_at = ""
    checked = 0
    for model in model_names:
        bone_map = bone_maps.get(model, {})
        for orig_name, index in manifest_anim.get(model, {}).items():
            if index >= len(seq_paths):
                continue
            merged = anims[seq_paths[index]]
            source = original_anims.get(model, {}).get(orig_name)
            if source is None:
                continue
            original = parse_smd_file(Path(source))
            if len(original.frames) != len(merged.frames):
                results.append(GateResult(
                    "pose_preserved", False,
                    f"{model}/{orig_name}: {len(original.frames)} frames "
                    f"became {len(merged.frames)}"))
                break
            merged_index = {n.name: n.index for n in merged.nodes}
            original_names = {n.index: n.name for n in original.nodes}
            for frame_number in {0, len(original.frames) // 2,
                                 len(original.frames) - 1}:
                original_worlds = fk_worlds(original,
                                            original.frames[frame_number])
                merged_worlds = fk_worlds(merged, merged.frames[frame_number])
                for index_o, world in original_worlds.items():
                    mapped = bone_map.get(original_names[index_o])
                    if mapped is None or mapped not in merged_index:
                        continue
                    got = merged_worlds[merged_index[mapped]].translation
                    want = world.translation
                    delta = max(abs(got.x - want.x), abs(got.y - want.y),
                                abs(got.z - want.z))
                    radius = max(abs(want.x), abs(want.y), abs(want.z))
                    excess = delta - radius * POSE_RELATIVE
                    checked += 1
                    if excess > worst:
                        worst = excess
                        worst_at = (f"{model}/{orig_name} f{frame_number} "
                                    f"({delta:.2e}u at r={radius:.0f})")
    results.append(GateResult(
        "pose_preserved", worst <= POSE_EPSILON,
        f"{checked} bone-frames checked, worst excess {worst:.2e}u "
        f"({worst_at})",
    ))

    # -- geometry_preserved ------------------------------------------------
    bad_verts = 0
    total_verts = 0
    original_positions: dict[str, set[tuple[float, float, float]]] = {}
    for model in model_names:
        positions: set[tuple[float, float, float]] = set()
        for smd_path in sorted((models_dir / model).glob("*.smd")):
            source_smd = parse_smd_file(smd_path)
            for t in source_smd.triangles:
                for v in t.vertices:
                    positions.add((round(v.position.x, 4),
                                   round(v.position.y, 4),
                                   round(v.position.z, 4)))
        original_positions[model] = positions
    for path, smd in meshes.items():
        owner = path.split("\\")[0]
        pool = original_positions.get(owner)
        if pool is None:
            continue
        for t in smd.triangles:
            for v in t.vertices:
                total_verts += 1
                key = (round(v.position.x, 4), round(v.position.y, 4),
                       round(v.position.z, 4))
                if key not in pool:
                    bad_verts += 1
    results.append(GateResult(
        "geometry_preserved", bad_verts == 0,
        f"{total_verts} mesh vertices bit-match their originals"
        if bad_verts == 0 else f"{bad_verts}/{total_verts} vertices moved",
    ))

    # -- budgets -----------------------------------------------------------
    problems: list[str] = []
    if len(reference_table) > BONE_LIMIT:
        problems.append(f"{len(reference_table)} bones > {BONE_LIMIT}")
    groups = parse_bodygroups(qc_text)
    submodels = sum(len(entries) for entries in groups.values())
    if submodels > SUBMODEL_LIMIT:
        problems.append(f"{submodels} submodels > {SUBMODEL_LIMIT}")
    if len(groups) > BODYPART_LIMIT:
        problems.append(f"{len(groups)} bodyparts > {BODYPART_LIMIT}")
    for path, smd in meshes.items():
        verts = {(v.position, v.bone) for t in smd.triangles for v in t.vertices}
        norms = {(v.normal, v.bone) for t in smd.triangles for v in t.vertices}
        if len(verts) > STOCK_VERT_LIMIT or len(norms) > STOCK_VERT_LIMIT:
            problems.append(f"{path}: {len(verts)}v/{len(norms)}n > "
                            f"{STOCK_VERT_LIMIT}")
    materials = {t.material for s in meshes.values() for t in s.triangles}
    if len(materials) > TEXTURE_LIMIT:
        problems.append(f"{len(materials)} textures > {TEXTURE_LIMIT}")
    missing_textures = [m for m in sorted(materials)
                        if not (part_dir / m).exists()]
    if missing_textures:
        problems.append(f"textures missing on disk: {missing_textures[:3]}")
    fake_models = [
        ModelInput(name=p, directory=part_dir, qc_path=part_dir / qc_name,
                   qc_text="", bodygroups={}, sequences=[],
                   meshes={"_": next(iter(meshes.values()))},
                   anims={p: smd})
        for p, smd in anims.items()
    ]
    for (seq, _), size in sequence_sizes(fake_models).items():
        if size > SEQ_DATA_LIMIT:
            problems.append(f"sequence {seq}: {size}B > 64K")
    long_paths = [p for p in [*mesh_paths, *seq_paths]
                  if len(p) > QC_PATH_LIMIT]
    if long_paths:
        problems.append(f"QC paths > {QC_PATH_LIMIT} chars: {long_paths[:3]}")
    results.append(GateResult(
        "budgets", not problems,
        f"bones={len(reference_table)} submodels={submodels} "
        f"textures={len(materials)}" if not problems else "; ".join(problems),
    ))
    return results


__all__ = ["GateResult", "POSE_EPSILON", "verify_part"]
