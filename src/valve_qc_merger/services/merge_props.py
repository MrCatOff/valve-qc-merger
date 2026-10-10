"""merge-props as a service: any models — effects, props, projectiles, and
view models whose hands ARE the model (zombie claws) or that have no hands —
merged into a few models, ``pev_body`` choosing the model.

No hand canonicalisation: every model keeps its own rig. Bones are shared by
name (the merged skeleton's first owner fixes a bone's parent; a model that
hangs it elsewhere is re-solved onto it per frame, world poses unchanged —
merge-v's ``unify_skeletons``), so models built on one rig (CSO claws, one
author's effects) share their bones. Each model's always-on bodygroups become
one submodel of the ``weapon`` group (more under the 2048-vertex cap go to
``weapon_2``…, blank for the others); alternatives of a bodygroup are dropped.
Sequences keep their labels (``<model>__<label>`` on a clash), the manifest
lists them in the source order; textures are deduped by content, render
modes and up to 4 attachments (shared slot bones) carried.

Parts close on studiomdl's and the engine's limits: 32 submodels per group,
127 bones, the texture budget, 255 sequences and a ``pev_body`` under 256
(the engine sends ``body`` in 8 bits). A model that fits no part on its own
ships as it is (``standalone/``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger import limits
from valve_qc_merger.merge_view.attachments import attachment_slots
from valve_qc_merger.merge_view.bodygroups import VERTEX_BUDGET, ModelParts
from valve_qc_merger.merge_view.decimate import unique_vertices
from valve_qc_merger.merge_view.discovery import (
    MergeViewError,
    ModelInput,
    discover_models,
    load_model,
    sanitize_model_dir,
)
from valve_qc_merger.merge_view.merger import (
    BONE_LIMIT,
    MergeError,
    merge_models,
    merged_skeleton,
    write_manifest_data,
)
from valve_qc_merger.merge_view.parts import (
    SHARED_HANDS_SEQUENCE_BUDGET,
    TEXTURE_BUDGET,
    _part_counts,
    _sequence_keys,
    _texture_keys,
    part_bytes,
)
from valve_qc_merger.services.base import (
    EXIT_DISCOVERY,
    EXIT_FAIL,
    EXIT_OK,
    Reporter,
    ServiceResult,
)
from valve_qc_merger.services.standalone import Rejects
from valve_qc_merger.services.standalone import entry as standalone_entry
from valve_qc_merger.services.standalone import ship as ship_standalone


@dataclass
class MergePropsOptions:
    models_dir: Path
    out: Path
    name: str = "props"
    exclude: list[str] = field(default_factory=list)
    manifest_format: str = "ini"
    texture_budget: int = TEXTURE_BUDGET
    sequence_budget: int = SHARED_HANDS_SEQUENCE_BUDGET
    dry_run: bool = False
    # pev_body values a part may span: None = an entity's (limits.body_values,
    # the server's delta.lst); a view model's part (merge-v's no-hands) 256
    body_values: int | None = None
    # a view model's part (merge-v's no-hands): every bodygroup leads with a
    # blank (pev_body 0 draws nothing: the client prediction shows nothing)
    view_model: bool = False
    # a view model's part (merge-v's no-hands): every bodygroup leads with a
    # blank (pev_body 0 draws nothing: the client prediction shows nothing)
    view_model: bool = False
    # a model that fits no part on its own ships as it is (standalone/)
    standalone_rejects: bool = True


Pair = tuple[ModelInput, ModelParts]


def prop_parts(model: ModelInput) -> ModelParts:
    """Every bodygroup's first entry, packed into submodels under the
    2048-vertex cap; hands are part of the model (no hands entry)."""
    parts = ModelParts()
    stems: list[str] = []
    for group, entries in model.bodygroups.items():
        resolved = [s for s in entries if s in model.meshes]
        if not resolved:
            continue
        stems.append(resolved[0])
        if len(resolved) > 1:
            parts.dropped[group] = resolved[1:]
            parts.warnings.append(f"bodygroup {group!r}: only its first entry is kept "
                                  f"({len(resolved) - 1} other(s) dropped)")
    current: list[str] = []
    verts = normals = 0
    for stem in stems:
        mesh = model.meshes[stem]
        more = unique_vertices(mesh)
        more_normals = len({(v.normal, v.bone, t.material.lower())  # as studiomdl counts
                            for t in mesh.triangles for v in t.vertices})
        if current and (verts + more > VERTEX_BUDGET
                        or normals + more_normals > VERTEX_BUDGET):
            parts.weapon_stems.append(current)
            current, verts, normals = [], 0, 0
        current.append(stem)
        verts += more
        normals += more_normals
    if current:
        parts.weapon_stems.append(current)
    return parts


ANCHOR = "vqm_anchor"


def repair_inputs(model_dir: Path) -> list[str]:
    """Make a staged model compilable on its own and mergeable:

    - a material with no texture file gets a small grey placeholder (the
      decompiler names a texture-less mesh ``missing.bmp``);
    - a model without any mesh (``muzzle_*``: bones, an attachment and a
      hitbox) gets an invisible one — a zero-area triangle on every bone an
      ``$attachment`` / ``$hbox`` names, so studiomdl keeps those bones
      (it drops vertex-less bones, and an attachment on a dropped bone is a
      hard error).

    Returns what was added."""
    import re

    from valve_qc_merger.merge_view.bmp8 import Bmp8, write_bmp8
    from valve_qc_merger.parsers.smd import parse_smd_file
    from valve_qc_merger.writers.smd import write_smd_file
    qc = next(model_dir.glob("*.qc"))
    text = qc.read_text(encoding="latin-1")
    notes: list[str] = []
    grey = write_bmp8(Bmp8(8, 8, [(128, 128, 128)] * 256, bytearray(64)))
    # studiomdl reads textures from $cdtexture (decompiles: ./maps_8bit) only
    cdtexture = re.search(r'(?im)^\s*\$cdtexture\s+"?([^"\s]+)"?', text)
    texture_dir = model_dir / cdtexture.group(1) if cdtexture else model_dir
    texture_dir.mkdir(parents=True, exist_ok=True)
    studio = re.findall(r'(?im)^\s*(?:\$body\s+\S+\s+|studio\s+)"?([^"\s]+)"?', text)
    if not studio:
        seq = re.search(r'(?is)\$sequence\s+\S+\s*\{?\s*"([^"]+)"', text)
        anim = None
        if seq:
            path = model_dir / (seq.group(1) + ("" if seq.group(1).lower().endswith(".smd")
                                                else ".smd"))
            anim = parse_smd_file(path) if path.is_file() else None
        if anim is not None and anim.frames:
            from valve_qc_merger.models.geometry import Vector2, Vector3
            from valve_qc_merger.models.smd import Smd, Triangle, Vertex
            index = {n.name: n.index for n in anim.nodes}
            named = re.findall(r'(?im)^\s*\$(?:attachment\s+\d+|hbox\s+\d+)\s+"?([^"\s]+)"?',
                               text)
            bones = [index[b] for b in dict.fromkeys(named) if b in index] or [0]
            from valve_qc_merger.merge_view.skeleton_ops import fk_worlds
            worlds = fk_worlds(anim, anim.frames[0])
            triangles = []
            for bone in bones:
                spot = worlds[bone].translation
                vertex = Vertex(bone=bone, position=Vector3(spot.x, spot.y, spot.z),
                                normal=Vector3(0.0, 0.0, 1.0), uv=Vector2(0.0, 0.0))
                triangles.append(Triangle(f"{ANCHOR}.bmp", (vertex, vertex, vertex)))
            write_smd_file(Smd(nodes=list(anim.nodes), frames=[anim.frames[0]],
                               triangles=triangles), model_dir / f"{ANCHOR}.smd")
            text = text.replace("$cliptotextures", f'$cliptotextures\n$body "{ANCHOR}" '
                                f'"{ANCHOR}"', 1) if "$cliptotextures" in text else (
                f'$body "{ANCHOR}" "{ANCHOR}"\n' + text)
            qc.write_text(text, encoding="latin-1")
            (texture_dir / f"{ANCHOR}.bmp").write_bytes(grey)
            notes.append(f"no mesh: an invisible anchor keeps {len(bones)} bone(s)")
    from valve_qc_merger.merge_view.merger import _find_texture
    for smd_path in sorted(model_dir.glob("*.smd")):
        materials = set()
        section, count = "", 0
        for line in smd_path.read_text(encoding="latin-1").splitlines():
            stripped = line.strip()
            if not section:
                section = stripped if stripped in ("nodes", "skeleton", "triangles") else ""
                count = 0
                continue
            if stripped == "end":
                section = ""
            elif section == "triangles":
                if count % 4 == 0 and stripped:
                    materials.add(stripped)
                count += 1
        for material in sorted(materials):
            if _find_texture(model_dir, material) is None:
                name = material if material.lower().endswith(".bmp") else material + ".bmp"
                (texture_dir / name).write_bytes(grey)
                notes.append(f"texture {material!r} missing: grey placeholder")
    return notes


def body_range(part: list[Pair], blank_first: bool = False) -> int:
    """Values ``pev_body`` spans for ``part`` in merge_models' layout:
    ``weapon`` (one entry per model) times each extra ``weapon_k`` group
    (blank + the models with a k-th submodel)."""
    total = len(part) + (1 if blank_first else 0)
    groups = max(len(parts.weapon_stems) for _m, parts in part)
    for k in range(1, groups):
        total *= 1 + sum(1 for _m, parts in part if len(parts.weapon_stems) > k)
    return total


def split_prop_parts(pairs: list[Pair], *, textures: int = TEXTURE_BUDGET,
                     sequences: int = SHARED_HANDS_SEQUENCE_BUDGET,
                     body_values: int | None = None,
                     blank_first: bool = False) -> list[list[Pair]]:
    """Greedy parts within every budget (see the module). A model that is
    over a budget on its own still gets a part of its own; the caller
    decides whether to ship it."""
    texture_keys = {m.name: _texture_keys(m, p) for m, p in pairs}
    seq_keys = {m.name: _sequence_keys(m) for m, _p in pairs}

    def fits(part: list[Pair]) -> bool:
        submodels, texcount, seqcount = _part_counts(part, texture_keys, seq_keys)
        if submodels > limits.submodels() or texcount > textures or seqcount > sequences:
            return False
        if body_range(part, blank_first) > (body_values or limits.body_values()):
            return False
        if part_bytes(part, texture_keys) > limits.PART_BYTES:
            return False
        models = [m for m, _p in part]
        return len(merged_skeleton(models)) + attachment_slots(models) <= BONE_LIMIT

    out: list[list[Pair]] = []
    current: list[Pair] = []
    for pair in pairs:
        trial = current + [pair]
        if current and not fits(trial):
            out.append(current)
            current = [pair]
        else:
            current = trial
    if current:
        out.append(current)
    return out


def alone_problem(pair: Pair, opts: MergePropsOptions) -> str | None:
    """Why ``pair`` cannot be merged even on its own, if it cannot."""
    model, parts = pair
    if not parts.weapon_stems:
        return "no mesh"
    if len(parts.weapon_stems) > limits.submodels():
        return f"{len(parts.weapon_stems)} submodels"
    bones = len(merged_skeleton([model])) + attachment_slots([model])
    if bones > BONE_LIMIT:
        return f"{bones} bones"
    if len(model.anims) > opts.sequence_budget:
        return f"{len(model.anims)} sequences"
    return None


def run_merge_props(opts: MergePropsOptions,
                    reporter: Reporter | None = None) -> ServiceResult:
    """Merge every decompiled model under ``opts.models_dir``."""
    reporter = reporter or Reporter()
    result = ServiceResult()
    try:
        model_dirs = discover_models(opts.models_dir, exclude=set(opts.exclude))
    except MergeViewError as exc:
        reporter.log(f"error: {exc}")
        result.exit_code = EXIT_DISCOVERY
        return result
    rejects = Rejects(opts.standalone_rejects, {d.name: d for d in model_dirs})
    pairs: list[Pair] = []
    inventory: list[dict[str, object]] = []
    for done, model_dir in enumerate(model_dirs):
        reporter.check()
        reporter.progress(done, len(model_dirs), model_dir.name)
        sanitize_model_dir(model_dir)
        repaired = repair_inputs(model_dir)
        try:
            model = load_model(model_dir, require_anims=False)
        except MergeViewError as exc:
            rejects.reject(result, reporter, model_dir.name, str(exc), "FAIL",
                           f"not merged: {exc}")
            continue
        parts = prop_parts(model)
        parts.warnings[:0] = repaired
        problem = alone_problem((model, parts), opts)
        if problem is not None:
            rejects.reject(result, reporter, model.name,
                           f"model {model.name!r}: {problem} — too big to merge",
                           "TOO-BIG", f"too big to merge: {problem}")
            continue
        pairs.append((model, parts))
        inventory.append({"name": model.name, "bones": len(model.bone_names),
                          "submodels": len(parts.weapon_stems),
                          "sequences": len(model.anims), "warnings": parts.warnings})
        warn = f"  ({len(parts.warnings)} warnings)" if parts.warnings else ""
        reporter.log(f"  {model.name:<24} OK    bones={len(model.bone_names):<4} "
                     f"submodels={len(parts.weapon_stems)} "
                     f"sequences={len(model.anims)}{warn}")
    reporter.progress(len(model_dirs), len(model_dirs), "loaded")
    reporter.log(f"  {len(pairs)} models loaded, {len(result.failures)} failed")
    opts.out.mkdir(parents=True, exist_ok=True)
    (opts.out / "inventory.json").write_text(
        json.dumps({"models": inventory, "failures": result.failures}, indent=1))
    result.data["inventory"] = inventory
    if opts.dry_run:
        result.exit_code = EXIT_FAIL if result.failures else EXIT_OK
        return result

    parts_list = split_prop_parts(pairs, textures=opts.texture_budget,
                                  sequences=opts.sequence_budget,
                                  body_values=opts.body_values,
                                  blank_first=opts.view_model) if pairs else []
    multi = len(parts_list) > 1
    result.data["parts"] = len(parts_list)
    aggregate: dict[str, dict[str, object]] = {}
    for number, part in enumerate(parts_list, 1):
        reporter.check()
        reporter.progress(number - 1, len(parts_list), f"part {number}")
        part_name = f"{opts.name}_p{number}" if multi else opts.name
        part_out = opts.out / f"p{number}" if multi else opts.out
        try:
            report = merge_models(part, part_out, part_name, blank_first=opts.view_model,
                                  manifest_format=opts.manifest_format,
                                  write_manifest=not multi)
        except MergeError as exc:
            reporter.log(f"error: merge failed ({part_name}): {exc}")
            result.exit_code = EXIT_FAIL
            return result
        result.outputs.append(part_out / f"{part_name}.qc")
        result.warnings.extend(report.warnings)
        reporter.log(f"  {part_name}: {len(part)} models, bones={report.bones} "
                     f"bodyparts={report.bodyparts} sequences={report.sequences} "
                     f"textures={report.textures}")
        for warning in report.warnings:
            reporter.log(f"    warn: {warning}")
        for model, _parts in part:
            aggregate[model.name] = {"model": f"{part_name}.mdl",
                                     **report.manifest[model.name]}
    if multi:
        write_manifest_data(opts.out, aggregate, opts.manifest_format)
    result.manifest = aggregate
    ship_standalone(rejects, opts.out, opts.manifest_format, result, reporter,
                    make_entry=lambda d, n, r: standalone_entry(d, n, r))
    result.exit_code = EXIT_FAIL if result.failures else EXIT_OK
    return result


__all__ = ["MergePropsOptions", "body_range", "prop_parts", "run_merge_props",
           "split_prop_parts"]
