"""Post-merge verification gate for merge-players.

Re-proves every claim from the EMITTED part files against the pristine inputs:

- ``skeleton_subset_of_donor`` — every emitted bone name is a donor bone, so the
  donor animations drive it by name;
- ``no_vertexless_leaf`` — no leaf bone across the skins is unused by any vertex
  (studiomdl prunes such bones and corrupts mesh strips doing so — the reason we
  pre-prune);
- ``geometry_preserved`` — each skin's vertex (position, normal, uv) set matches
  its source body mesh (sub-bone collapse only re-binds bones, never moves verts);
- ``sequences_canonical`` — emitted ``$sequence`` names+order equal the donor's,
  and every voided sequence points at the placeholder;
- ``budgets`` — bones <= 127, submodels <= limit, textures <= 100.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path

from valve_qc_merger.merge_players.discovery import Donor
from valve_qc_merger.merge_players.sequences import (
    DEFAULT_PLACEHOLDER_GLOBS,
    PLACEHOLDER_STEM,
)
from valve_qc_merger.merge_view.verify import (
    BONE_LIMIT,
    TEXTURE_LIMIT,
    GateResult,
)
from valve_qc_merger.models.smd import Smd
from valve_qc_merger.parsers.smd import parse_smd_file

_STUDIO_RE = re.compile(r'studio\s+"([^"]+)"')
_SEQ_NAME_RE = re.compile(r'\$sequence\s+"([^"]+)"')


def _vsig(smd: Smd) -> set[tuple]:
    """Signature of vertex (position, normal), rounded to 5 dp (bone-agnostic).

    UV is excluded: ``--pack-textures`` legitimately remaps UVs into an atlas,
    whereas positions and normals are never transformed by the merge.
    """
    def r(v: tuple) -> tuple:
        return tuple(round(c, 5) for c in v)
    return {
        (r(v.position), r(v.normal))
        for t in smd.triangles for v in t.vertices
    }


def _seq_first_source(qc_text: str, name: str) -> str | None:
    """The first quoted anim path inside ``$sequence "name" { ... }`` (brace-aware)."""
    m = re.search(r'\$sequence\s+"' + re.escape(name) + r'"\s*\{', qc_text)
    if m is None:
        return None
    depth, i = 0, m.end() - 1
    while i < len(qc_text):
        ch = qc_text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = qc_text[m.end():i]
    src = re.search(r'"([^"]+)"', body)
    return src.group(1) if src else None


def verify_players_part(
    part_dir: Path,
    qc_name: str,
    donor: Donor,
    skins: list[tuple[str, Path, list[str]]],
    *,
    placeholder_globs: tuple[str, ...] = DEFAULT_PLACEHOLDER_GLOBS,
    submodel_limit: int = 32,
    texture_count: int = 0,
) -> list[GateResult]:
    """Run every gate check for one emitted part; one row per check."""
    results: list[GateResult] = []
    qc_text = (part_dir / qc_name).read_text(encoding="latin-1")
    donor_names = [name for name, _ in donor.table]

    mesh_stems = [s for s in _STUDIO_RE.findall(qc_text) if s.startswith("geometry/")]
    emitted = {stem: parse_smd_file(part_dir / f"{stem}.smd") for stem in mesh_stems}

    # skeleton_subset_of_donor
    all_names: set[str] = set()
    union_parent: dict[str, str | None] = {}
    used_names: set[str] = set()
    for smd in emitted.values():
        name_of = {n.index: n.name for n in smd.nodes}
        for n in smd.nodes:
            all_names.add(n.name)
            union_parent.setdefault(n.name, name_of.get(n.parent) if n.parent >= 0 else None)
        used_names |= {name_of[v.bone] for t in smd.triangles for v in t.vertices}
    foreign = sorted(all_names - set(donor_names))
    results.append(GateResult(
        "skeleton_subset_of_donor", not foreign,
        f"{len(all_names)} bones, all donor"
        if not foreign else f"non-donor bones: {', '.join(foreign)}",
    ))

    # no_vertexless_leaf (studiomdl would prune -> strip corruption)
    parents = set(union_parent.values())
    dangling = sorted(
        name for name in all_names if name not in parents and name not in used_names
    )
    results.append(GateResult(
        "no_vertexless_leaf", not dangling,
        "every leaf bone is weighted"
        if not dangling else f"vertexless leaves: {', '.join(dangling)}",
    ))

    # geometry_preserved (a skin's parts may be several submodels geometry/<name>_pN)
    bad: list[str] = []
    for name, directory, stems in skins:
        want: set[tuple] = set()
        for stem in stems:
            src = directory / (stem if stem.lower().endswith(".smd") else f"{stem}.smd")
            if src.exists():
                want |= _vsig(parse_smd_file(src))
        got: set[tuple] = set()
        for stem, smd in emitted.items():
            if stem == f"geometry/{name}" or stem.startswith(f"geometry/{name}_p"):
                got |= _vsig(smd)
        if want and not want <= got:
            bad.append(name)
    results.append(GateResult(
        "geometry_preserved", not bad,
        "all skins bit-match source"
        if not bad else f"vertices changed: {', '.join(bad)}",
    ))

    # sequences_canonical
    emitted_names = _SEQ_NAME_RE.findall(qc_text)
    donor_seq = donor_names_seq(donor.qc_text)
    order_ok = emitted_names == donor_seq
    placeholdered = [n for n in donor_seq
                     if any(fnmatch(n, g) for g in placeholder_globs)]
    voided_ok = all(
        (_seq_first_source(qc_text, name) or "").endswith(PLACEHOLDER_STEM)
        for name in placeholdered
    )
    results.append(GateResult(
        "sequences_canonical", order_ok and voided_ok,
        f"{len(emitted_names)} sequences, order "
        + ("== donor" if order_ok else "!= donor")
        + ("" if voided_ok else "; a voided slot lost its placeholder"),
    ))

    # texture_names — long / non-ASCII-safe material names SIGTRAP studiomdl
    materials = {t.material for smd in emitted.values() for t in smd.triangles}
    unsafe = sorted(m for m in materials
                    if len(m) > 40 or not re.match(r"^[A-Za-z0-9_.-]+$", m))
    results.append(GateResult(
        "texture_names", not unsafe,
        f"{len(materials)} materials, all studiomdl-safe"
        if not unsafe else f"unsafe (long/special): {', '.join(unsafe)[:80]}",
    ))

    # budgets — total submodels include the leading blank of every bodygroup
    n_sub = len(re.findall(r"^\s*studio\s", qc_text, re.M)) + \
        len(re.findall(r"^\s*blank\s*$", qc_text, re.M))
    ok = (len(all_names) <= BONE_LIMIT and n_sub <= submodel_limit
          and texture_count <= TEXTURE_LIMIT)
    results.append(GateResult(
        "budgets", ok,
        f"bones={len(all_names)}/{BONE_LIMIT} submodels={n_sub}/{submodel_limit} "
        f"textures={texture_count}/{TEXTURE_LIMIT}",
    ))
    return results


def donor_names_seq(qc_text: str) -> list[str]:
    """Ordered donor sequence names (independent of blend/loop bodies)."""
    return re.findall(r'\$sequence\s+"([^"]+)"', qc_text)


__all__ = ["verify_players_part"]
