"""Emit one merged players part: a skin bodygroup on the donor rig + shared anims.

Each source body becomes one submodel of a single ``skin`` bodygroup (no leading
blank — a player always renders a body; ``pev_body`` selects the skin). The
donor supplies the skeleton, hitboxes, attachments, controller and the canonical
sequence set (unneeded slots voided with the placeholder). Textures are staged
and deduped with the shared merge-view helpers. QC paths use forward slashes, so
the output compiles as-is on the native macOS studiomdl (no backslash fixup).
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from valve_qc_merger.merge_players.discovery import (
    Donor,
    PlayerModel,
    bind_positions,
    donor_body_mesh,
)
from valve_qc_merger.merge_players.sequences import build_sequences
from valve_qc_merger.merge_players.skeleton import reduce_body
from valve_qc_merger.merge_view.atlas import (
    TextureOptions,
    downscale_textures,
    pack_textures,
)
from valve_qc_merger.merge_view.merger import (
    BONE_LIMIT,
    STOCK_VERT_LIMIT,
    SUBMODEL_TRI_WARN,
    TEXTURE_WARN,
    MergeError,
    MergeReport,
    _collect_render_modes,
    _stage_textures,
    write_manifest_data,
)
from valve_qc_merger.models.smd import Smd
from valve_qc_merger.writers.smd import write_smd_text

# Donor QC directives carried over, filtered to bones that survive pruning.
_DIRECTIVE_RE = re.compile(
    r"^\s*\$(?:hbox|attachment|controller|cbox|bbox|flags)\b.*$", re.MULTILINE
)
_DIRECTIVE_BONE_RE = re.compile(r'^\s*\$\w+\s+\S+\s+"([^"]+)"')


def _clone(mesh: Smd) -> Smd:
    """Shallow copy: skeleton edits reassign the list attributes, so the frozen
    node/frame/triangle elements can be shared without touching the source."""
    return Smd(version=mesh.version, nodes=list(mesh.nodes),
               frames=list(mesh.frames), triangles=list(mesh.triangles))


def _donor_skin(donor: Donor) -> PlayerModel:
    """Wrap the donor body as a skin (skin 0 under ``--include-base``)."""
    return PlayerModel(
        name="base", directory=donor.directory, qc_path=donor.qc_path,
        qc_text=donor.qc_text, body_meshes=[donor_body_mesh(donor)],
        body_stems=[donor.body_stem], hitbox_sig="", height=0.0,
    )


# studiomdl copies texture names into a fixed char buffer; a long name (CSO's
# "…Copyright(C)[2008]NEXON&…bmp") plus a conflict prefix overflows it → SIGTRAP.
_TEXNAME_LIMIT = 40
_TEXNAME_SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _shorten_textures(
    out_dir: Path, kept: dict[str, list[Smd]], staged_names: list[str],
    render_modes: dict[str, str], report: MergeReport,
) -> list[str]:
    """Rename over-long / non-ASCII-safe staged textures to compact names.

    Renames the files on disk and rewrites every reference (mesh materials,
    render modes) so studiomdl never sees a name that overflows its buffer.
    """
    remap: dict[str, str] = {}
    used = {n.lower() for n in staged_names}
    for name in staged_names:
        if len(name) <= _TEXNAME_LIMIT and _TEXNAME_SAFE.match(name):
            continue
        base = re.sub(r"[^A-Za-z0-9]+", "_", name.rsplit(".", 1)[0])[:24].strip("_") or "tex"
        cand = f"{base}.bmp"
        counter = 1
        while cand.lower() in used:
            counter += 1
            cand = f"{base}_{counter}.bmp"
        remap[name] = cand
        used.add(cand.lower())
    if not remap:
        return staged_names
    for old, new in remap.items():
        src = out_dir / old
        if src.exists():
            src.rename(out_dir / new)
    for smds in kept.values():
        for smd in smds:
            smd.triangles = [
                dataclasses.replace(t, material=remap.get(t.material, t.material))
                for t in smd.triangles
            ]
    for old in list(render_modes):
        if old in remap:
            render_modes[remap[old]] = render_modes.pop(old)
    report.warnings.append(f"shortened {len(remap)} over-long texture name(s)")
    return [remap.get(n, n) for n in staged_names]


def _donor_directives(qc_text: str, bones: set[str]) -> list[str]:
    """Donor $hbox/$attachment/$controller/... lines whose bone survived pruning."""
    kept: list[str] = []
    for m in _DIRECTIVE_RE.finditer(qc_text):
        line = m.group(0).strip()
        bone = _DIRECTIVE_BONE_RE.match(line)
        if bone is None or bone.group(1) in bones:
            kept.append(line)
    return kept


def merge_players_part(
    models: list[PlayerModel],
    donor: Donor,
    out_dir: Path,
    name: str,
    *,
    include_base: bool = False,
    placeholder_globs: tuple[str, ...] = ("*shield*",),
    submodel_limit: int = 32,
    textures: TextureOptions | None = None,
    manifest_format: str = "ini",
    write_manifest: bool = True,
) -> MergeReport:
    """Write one merged players part; returns the budget report."""
    report = MergeReport()
    skins: list[PlayerModel] = ([_donor_skin(donor)] if include_base else []) + models
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "geometry").mkdir(exist_ok=True)

    # --- reduce every body part to the bones it needs (donor names) ---------
    # Each source body PART stays its own submodel (not concatenated): a
    # high-poly body is split across parts to fit studiomdl's 2048-vert/submodel
    # cap, so we preserve that split. studiomdl unions the parts' bones by name.
    kept: dict[str, list[Smd]] = {}
    surviving: set[str] = set()
    for skin in skins:
        parts: list[Smd] = []
        collapsed: set[str] = set()
        for part in skin.body_meshes:
            work = _clone(part)
            try:
                collapsed.update(reduce_body(work, donor))
            except (ValueError, RuntimeError) as exc:
                raise MergeError(f"{skin.name}: cannot reduce body: {exc}") from exc
            parts.append(work)
            surviving |= {n.name for n in work.nodes}
        if collapsed:
            report.warnings.append(
                f"{skin.name}: collapsed sub-bones {', '.join(sorted(collapsed))}"
            )
        kept[skin.name] = parts

    part_slots = max((len(v) for v in kept.values()), default=1)
    # body0 = every skin (no blank); each extra slot = blank + its owners only.
    total_submodels = len(skins) + sum(
        1 + sum(1 for s in skins if len(kept[s.name]) > k)
        for k in range(1, part_slots)
    )
    if total_submodels > submodel_limit:
        raise MergeError(
            f"{total_submodels} submodels exceed the {submodel_limit} limit; split "
            "into more parts (or raise --submodel-limit for a patched compiler)"
        )
    report.bones = len(surviving)
    if report.bones > BONE_LIMIT:
        report.warnings.append(f"merged skeleton has {report.bones} bones (>{BONE_LIMIT})")

    # --- textures ----------------------------------------------------------
    staged_renames, staged_names = _stage_textures(out_dir, skins, kept, report)
    render_modes = _collect_render_modes(skins, kept, staged_renames, staged_names, report)
    staged_names = _shorten_textures(out_dir, kept, staged_names, render_modes, report)
    report.textures = len(staged_names)
    if textures is not None and textures.max_size is not None:
        downscale_textures(out_dir, staged_names, textures.max_size, report.warnings)
    if textures is not None and textures.pack:
        flat = [smd for smds in kept.values() for smd in smds]
        report.atlas = pack_textures(
            out_dir, flat, staged_names, render_modes, textures, report.warnings,
        )
        atlas_files = {t.split(":")[0] for t in report.atlas.values()}
        for packed in report.atlas:
            render_modes.pop(packed, None)
        for atlas_file in sorted(atlas_files):
            if atlas_file.startswith("atlasm"):
                render_modes[atlas_file] = "masked"
        report.textures = len(staged_names) - len(report.atlas) + len(atlas_files)
    if report.textures > TEXTURE_WARN:
        report.warnings.append(
            f"{report.textures} textures staged (studiomdl degrades past "
            f"~{TEXTURE_WARN}; lower the texture budget to split further)"
        )

    # --- geometry SMDs (one file per part) ---------------------------------
    report.bodyparts = part_slots
    for skin in skins:
        for k, mesh in enumerate(kept[skin.name]):
            (out_dir / "geometry" / f"{skin.name}_p{k}.smd").write_text(
                write_smd_text(mesh), encoding="latin-1"
            )
            verts = {(v.position, v.bone) for t in mesh.triangles for v in t.vertices}
            if len(verts) > STOCK_VERT_LIMIT:
                report.warnings.append(
                    f"{skin.name}_p{k}: submodel has {len(verts)} verts (stock "
                    f"studiomdl caps {STOCK_VERT_LIMIT}; needs a raised-limit compiler)"
                )
            if len(mesh.triangles) > SUBMODEL_TRI_WARN:
                report.warnings.append(
                    f"{skin.name}_p{k}: submodel has {len(mesh.triangles)} tris "
                    f"(renderers degrade past ~{SUBMODEL_TRI_WARN})"
                )

    # --- canonical sequence set + shared anims (rebased to THIS group's rig) --
    # A representative member's bind lengths; every skin here shares the rig
    # (grouped by proportion), so the rebased animations fit all with no stretch.
    rep = max(models[0].body_meshes, key=lambda m: len(m.nodes)) if models else None
    group_bind = bind_positions(rep) if rep is not None else None
    seq = build_sequences(donor, out_dir, placeholder_globs=placeholder_globs,
                          bind_positions=group_bind)
    report.sequences = len(seq.names)
    report.warnings.extend(seq.warnings)

    # --- QC ----------------------------------------------------------------
    lines: list[str] = [
        "// Generated by valve-qc-merger merge-players: CSO bodies on the",
        f"// canonical CS 1.6 rig; {part_slots} body bodygroup(s), shared animations.",
        "// A skin is shown by setting pev_body to its manifest value (blank = 0).",
        "",
        f'$modelname "{name}.mdl"',
        '$cd "."',
        '$cdtexture "."',
        "$cliptotextures",
        "$scale 1.0",
        "",
    ]
    render_lines = [f'$texrendermode "{f}" {m}' for f, m in sorted(render_modes.items())]
    if render_lines:
        lines += [*render_lines, ""]

    # One bodygroup per part slot. body0 lists EVERY skin's first part (no blank,
    # every skin has one). Each extra slot lists ONLY the skins that own a part
    # there, behind a leading blank — no wasted blank rows. A skin's index in a
    # slot is its position among that slot's members (0 = the blank / "no part").
    slot_members = [
        [skin for skin in skins if len(kept[skin.name]) > k] for k in range(part_slots)
    ]
    slot_index: list[dict[str, int]] = []
    for k, members in enumerate(slot_members):
        lines.append(f'$bodygroup "body{k}"')
        lines.append("{")
        offset = 0 if k == 0 else 1  # slot 0 has no leading blank
        if k > 0:
            lines.append("\tblank")
        idx: dict[str, int] = {}
        for pos, skin in enumerate(members):
            lines.append(f'\tstudio "geometry/{skin.name}_p{k}"')
            idx[skin.name] = pos + offset
        slot_index.append(idx)
        lines.append("}")
        lines.append("")

    directives = _donor_directives(donor.qc_text, surviving)
    if directives:
        lines += [*directives, ""]
    lines += seq.qc_lines
    lines.append("")
    (out_dir / f"{name}.qc").write_text("\n".join(lines) + "\n", encoding="latin-1")

    # --- manifest: pev_body per skin (mixed radix over the actual slot sizes) --
    slot_len = [len(members) if k == 0 else 1 + len(members)
                for k, members in enumerate(slot_members)]
    base = [1]
    for k in range(1, part_slots):
        base.append(base[k - 1] * slot_len[k - 1])
    for skin in skins:
        indices = [slot_index[k].get(skin.name, 0) for k in range(part_slots)]
        body = sum(indices[k] * base[k] for k in range(part_slots))
        report.pev_body[skin.name] = body
        entry: dict[str, int | str] = {
            "pev_body": body, "model": f"{name}.mdl", "parts": len(kept[skin.name]),
        }
        for k in range(part_slots):
            entry[f"body{k}"] = indices[k]
        report.manifest[skin.name] = entry
    if write_manifest:
        write_manifest_data(
            out_dir, {k: dict(v) for k, v in report.manifest.items()}, manifest_format,
        )
    report.sequences_deduped = len(seq.placeholdered)  # reuse field: voided count
    return report


__all__ = ["merge_players_part"]
