"""Zombie-hands merge: canonical hand bones, a shared grenade, one QC.

Pipeline per input model (decompiled ``v_<zombie>_<knife|grenade>[_variant]``):

1. Split every mesh into HAND triangles and GRENADE triangles (material name
   matching the grenade texture prefix, ``frogbomb`` by default).
2. Prefix the grenade subtree (``Bone_Root``/``bomb_Root`` and everything
   under it) with ``gren_``: rigs disagree on the root's name, and the ghost
   knife rig reuses ``Bone_Root`` as its HAND root.
3. Rename hand bones onto one reference rig (the first knife model) with the
   merge-v correspondence engine, so the ``Bip01 L...``/``NEXON_CSO_...`` arm
   rigs share the ``Bone01...`` names and the skeleton stays under 127.
4. Unify every SMD onto one node table (merge-v ``unify_skeletons``: exact
   per-frame re-solves; each mesh keeps its own bind).
5. Dedupe hand meshes (knife and grenade hands are usually one mesh), keep
   ONE grenade mesh for everyone, stage textures by (bytes, render mode) so a
   texture used both opaque and additive (ghost's invisibility skill) gets a
   second copy, and write the QC + manifest.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger import limits
from valve_qc_merger.merge_view.animsize import SEQ_DATA_LIMIT, sequence_sizes
from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.merge_view.hands import collision_guard, match_hands, rig_bones_from_smd
from valve_qc_merger.merge_view.merger import (
    _sanitize_material,
    fit_texture_name,
    merged_skeleton,
    unify_skeletons,
    write_manifest_data,
)
from valve_qc_merger.merge_view.skeleton_ops import fk_worlds, rename_bones
from valve_qc_merger.models.smd import Frame, Smd, Triangle
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget.correspondence import CorrespondenceError
from valve_qc_merger.transform import Transform
from valve_qc_merger.writers.smd import write_smd_text

BONE_LIMIT = 127
SEQUENCE_LIMIT = 255  # the game selects a viewmodel animation by a byte
TEXTURE_LIMIT = 100
NAME_LIMIT = 31  # studiomdl strcpy's bone/sequence names into char[32]
GRENADE_PREFIX = "gren_"
POSE_EPSILON = 2e-3

_NAME_RE = re.compile(
    r"^v_(?P<zombie>.+?)_(?P<role>knife|grenade)(?:_(?P<variant>.+))?$",
    re.IGNORECASE,
)
_TEXRENDERMODE_RE = re.compile(
    r'^\s*\$texrendermode\s+"?([^"\r\n]+?)"?\s+(\w+)\s*$', re.MULTILINE,
)
_SOUND_EVENT_RE = re.compile(r'"([^"]+\.wav)"', re.IGNORECASE)


class ZhandsError(RuntimeError):
    """A merge-level failure (unparsable name, budget violation)."""


@dataclass
class ZombieModel:
    """One input model, split and renamed."""

    model: ModelInput
    zombie: str
    role: str  # "knife" | "grenade"
    variant: str | None
    render_modes: dict[str, str]  # material (lower) -> mode
    renames: dict[str, str] = field(default_factory=dict)  # original -> final bone
    hands: Smd | None = None
    grenade: Smd | None = None
    # its grenade animation redrawn for this model's frog (one frog for all)
    redrawn_by: str | None = None

    @property
    def stem(self) -> str:
        return self.model.name

    @property
    def tag(self) -> str:
        return self.stem[2:] if self.stem.lower().startswith("v_") else self.stem


@dataclass
class ZhandsReport:
    bones: int = 0
    hands: list[str] = field(default_factory=list)
    grenade_from: str | None = None
    sequences: int = 0
    sequences_deduped: int = 0
    textures: int = 0
    pev_body: dict[str, int] = field(default_factory=dict)
    manifest: dict[str, dict[str, int | str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    gate: list[tuple[str, bool, str]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Small SMD helpers
# --------------------------------------------------------------------------- #
def _bones_of(smd: Smd, triangles: list[Triangle]) -> set[str]:
    name_of = {n.index: n.name for n in smd.nodes}
    return {name_of[v.bone] for t in triangles for v in t.vertices}


def _ancestor_closure(smd: Smd, names: set[str]) -> set[str]:
    by_name = {n.name: n for n in smd.nodes}
    name_of = {n.index: n.name for n in smd.nodes}
    closed: set[str] = set()
    for name in names:
        cursor: str | None = name
        while cursor is not None and cursor not in closed:
            closed.add(cursor)
            parent = by_name[cursor].parent
            cursor = name_of.get(parent) if parent >= 0 else None
    return closed


def _combined_mesh(model: ModelInput) -> Smd:
    """Every mesh of the model as one SMD (decompiled bodies share a table)."""
    meshes = [m for m in model.meshes.values()]
    first = max(meshes, key=lambda m: len(m.nodes))
    table = [(n.index, n.name, n.parent) for n in first.nodes]
    triangles: list[Triangle] = []
    for mesh in meshes:
        if not mesh.triangles:
            continue
        if [(n.index, n.name, n.parent) for n in mesh.nodes] != table:
            raise ZhandsError(f"{model.name}: meshes disagree on the skeleton")
        triangles.extend(mesh.triangles)
    return Smd(nodes=list(first.nodes), frames=list(first.frames),
               triangles=triangles)


def _is_grenade(material: str, prefix: str) -> bool:
    return material.lower().startswith(prefix.lower())


def _render_modes(qc_text: str) -> dict[str, str]:
    return {name.lower(): mode for name, mode in _TEXRENDERMODE_RE.findall(qc_text)}


def _dominant_material(triangles: list[Triangle]) -> str:
    return Counter(t.material for t in triangles).most_common(1)[0][0]


def _prune_to(smd: Smd, keep: set[str]) -> Smd:
    """A copy carrying only ``keep`` bones (ancestor-closed) — for rig matching."""
    nodes = [n for n in smd.nodes if n.name in keep]
    kept = {n.index for n in nodes}
    frames = [Frame(f.time, tuple(p for p in f.poses if p.bone in kept))
              for f in smd.frames]
    return Smd(nodes=nodes, frames=frames, triangles=[])


# --------------------------------------------------------------------------- #
# Stage 1-3: split, grenade prefix, hand canonicalisation
# --------------------------------------------------------------------------- #
def prepare_model(model: ModelInput, grenade_prefix: str) -> ZombieModel:
    match = _NAME_RE.match(model.name)
    if match is None:
        raise ZhandsError(
            f"{model.name}: expected v_<zombie>_<knife|grenade>[_variant]"
        )
    zm = ZombieModel(
        model=model, zombie=match.group("zombie").lower(),
        role=match.group("role").lower(),
        variant=(match.group("variant") or None),
        render_modes=_render_modes(model.qc_text),
    )
    combined = _combined_mesh(model)
    hand_tris = [t for t in combined.triangles
                 if not _is_grenade(t.material, grenade_prefix)]
    gren_tris = [t for t in combined.triangles
                 if _is_grenade(t.material, grenade_prefix)]
    if not hand_tris:
        raise ZhandsError(f"{model.name}: no hand triangles")

    # Grenade subtree: from every grenade-weighted bone up to (excluding) the
    # first hand bone/ancestor, plus everything below those roots.
    renames: dict[str, str] = {}
    if gren_tris:
        hand_closure = _ancestor_closure(combined, _bones_of(combined, hand_tris))
        by_name = {n.name: n for n in combined.nodes}
        name_of = {n.index: n.name for n in combined.nodes}
        roots: set[str] = set()
        for bone in _bones_of(combined, gren_tris):
            if bone in hand_closure:
                continue
            cursor = bone
            while True:
                parent = by_name[cursor].parent
                parent_name = name_of.get(parent) if parent >= 0 else None
                if parent_name is None or parent_name in hand_closure:
                    roots.add(cursor)
                    break
                cursor = parent_name
        children: dict[str, list[str]] = {}
        for n in combined.nodes:
            if n.parent >= 0:
                children.setdefault(name_of[n.parent], []).append(n.name)
        subtree: set[str] = set()
        stack = sorted(roots)
        while stack:
            bone = stack.pop()
            if bone in subtree or bone in hand_closure:
                continue
            subtree.add(bone)
            stack.extend(children.get(bone, []))
        for bone in subtree:
            if len(roots) == 1 and bone in roots:
                renames[bone] = f"{GRENADE_PREFIX}root"
            else:
                renames[bone] = (GRENADE_PREFIX + bone)[:NAME_LIMIT]

    _rename_everywhere(zm, renames)
    combined = _combined_mesh(model)
    zm.hands = Smd(nodes=list(combined.nodes), frames=list(combined.frames),
                   triangles=[t for t in combined.triangles
                              if not _is_grenade(t.material, grenade_prefix)])
    gren = [t for t in combined.triangles if _is_grenade(t.material, grenade_prefix)]
    if gren:
        zm.grenade = Smd(nodes=list(combined.nodes),
                         frames=list(combined.frames), triangles=gren)
    return zm


def _rename_everywhere(zm: ZombieModel, renames: dict[str, str]) -> None:
    renames = {old: new for old, new in renames.items() if old != new}
    if not renames:
        return
    for smd in {**zm.model.meshes, **zm.model.anims}.values():
        rename_bones(smd, renames)
    for smd in (zm.hands, zm.grenade):
        if smd is not None:
            rename_bones(smd, renames)
    # Compose with earlier renames: zm.renames maps ORIGINAL -> current name.
    previous = dict(zm.renames)
    composed = {orig: renames.get(cur, cur) for orig, cur in previous.items()}
    current_names = set(previous.values())
    for old, new in renames.items():
        if old not in current_names and old not in previous:
            composed[old] = new
    zm.renames = composed


def canonicalize_hands(models: list[ZombieModel], report: ZhandsReport) -> str:
    """Rename every hand rig onto the reference (first knife model's) names.

    A rig that already carries every reference bone under the same parent is
    left alone (thumb detection can be ambiguous on a rig that needs nothing).
    Returns the reference model's name.
    """
    reference_model = next((m for m in models if m.role == "knife"), models[0])
    ref_hands = reference_model.hands
    assert ref_hands is not None
    ref_keep = _ancestor_closure(ref_hands, _bones_of(ref_hands, ref_hands.triangles))
    reference_smd = _prune_to(ref_hands, ref_keep)
    reference = rig_bones_from_smd(reference_smd)
    ref_parent = {b.name: b.parent for b in reference}

    for zm in models:
        if zm is reference_model:
            continue
        assert zm.hands is not None
        parent_of = {b.name: b.parent for b in rig_bones_from_smd(zm.hands)}
        if all(parent_of.get(name, "<missing>") == parent
               for name, parent in ref_parent.items()):
            continue
        dominant = _dominant_material(zm.hands.triangles)
        include = _bones_of(zm.hands, [t for t in zm.hands.triangles
                                       if t.material == dominant])
        try:
            match = match_hands(zm.hands, reference, include)
        except CorrespondenceError as exc:
            report.warnings.append(
                f"{zm.stem}: hand rig not matched to {reference_model.stem} "
                f"({exc}); its bones stay separate"
            )
            continue
        conflicts = collision_guard(zm.hands, match.renames)
        if conflicts:
            report.warnings.append(
                f"{zm.stem}: hand renames collide ({'; '.join(conflicts[:2])}); "
                "its bones stay separate"
            )
            continue
        _rename_everywhere(zm, match.renames)
    return reference_model.stem


# --------------------------------------------------------------------------- #
# Stage 5: dedupe, textures, QC
# --------------------------------------------------------------------------- #
MESH_TOLERANCE = 0.02  # units / UV: decompiled re-exports differ by ~1e-4


def _local_vertices(smd: Smd) -> list[tuple]:
    """Per vertex, in file order: (material, bone, bone-local position, uv)."""
    worlds = fk_worlds(smd, smd.frames[0])
    name_of = {n.index: n.name for n in smd.nodes}
    out = []
    for t in smd.triangles:
        for v in t.vertices:
            p = worlds[v.bone].inverse().transform_point(v.position)
            out.append((t.material.lower(), name_of[v.bone], p, v.uv))
    return out


def _same_mesh(a: Smd, b: Smd, modes_a: dict[str, str],
               modes_b: dict[str, str]) -> bool:
    """Same triangles (in file order), bones, render modes; positions in each
    bone's own bind frame and UVs within ``MESH_TOLERANCE``. Bone-local, so a
    mesh bound under two different rest poses still compares equal."""
    if len(a.triangles) != len(b.triangles):
        return False
    for (ma, ba, pa, ua), (mb, bb, pb, ub) in zip(
            _local_vertices(a), _local_vertices(b), strict=True):
        if ma != mb or ba != bb or modes_a.get(ma, "") != modes_b.get(mb, ""):
            return False
        if max(abs(pa.x - pb.x), abs(pa.y - pb.y), abs(pa.z - pb.z),
               abs(ua.u - ub.u), abs(ua.v - ub.v)) > MESH_TOLERANCE:
            return False
    return True


def _find_texture(directory: Path, material: str) -> Path | None:
    wanted = material.lower()
    for candidate in sorted(directory.rglob("*")):
        if candidate.is_file() and candidate.name.lower() == wanted:
            return candidate
    return None


class _TextureStage:
    """Stage textures by (content, render mode); returns final names."""

    def __init__(self, out_dir: Path, report: ZhandsReport) -> None:
        self.out_dir = out_dir
        self.report = report
        self.by_key: dict[tuple[str, str], str] = {}
        self.taken: set[str] = set()
        self.modes: dict[str, str] = {}

    def stage(self, directory: Path, material: str, mode: str | None) -> str:
        source = _find_texture(directory, material)
        if source is None:
            self.report.warnings.append(
                f"{directory.name}: texture {material!r} not found")
            return _sanitize_material(material)
        data = source.read_bytes()
        key = (hashlib.md5(data).hexdigest(), mode or "")
        if key in self.by_key:
            return self.by_key[key]
        base = _sanitize_material(material)
        stem, ext = base[:-4], base[-4:]
        name = base
        if mode and base.lower() in self.taken:
            name = f"{stem}_{mode}{ext}"
        counter = 2
        name = fit_texture_name(name)
        while name.lower() in self.taken:
            name = fit_texture_name(f"{stem}_{counter}{ext}")
            counter += 1
        self.taken.add(name.lower())
        self.by_key[key] = name
        (self.out_dir / name).write_bytes(data)
        if mode:
            self.modes[name] = mode
        return name


def _restage(smd: Smd, zm: ZombieModel, stage: _TextureStage) -> Smd:
    names: dict[str, str] = {}
    for material in sorted({t.material for t in smd.triangles}):
        names[material] = stage.stage(
            zm.model.directory, material, zm.render_modes.get(material.lower()))
    return Smd(nodes=list(smd.nodes), frames=list(smd.frames), triangles=[
        dataclasses.replace(t, material=names[t.material]) for t in smd.triangles
    ])


RIG_TOLERANCE = 0.05  # units / radians-ish: decompiled re-exports differ by ~1e-4


def _grenade_rig(zm: ZombieModel) -> dict[str, Transform]:
    """The grenade's bones in its root's frame, at the mesh's bind."""
    smd = zm.grenade
    worlds = fk_worlds(smd, smd.frames[0])
    name_of = {n.index: n.name for n in smd.nodes}
    parent_of = {n.index: n.parent for n in smd.nodes}
    gren = [i for i, n in name_of.items() if n.startswith(GRENADE_PREFIX)]
    roots = [i for i in gren if parent_of[i] not in gren]
    if not roots:
        return {}
    inverse = worlds[min(roots, key=lambda i: name_of[i])].inverse()
    return {name_of[i]: inverse.compose(worlds[i]) for i in gren}


def _same_grenade_rig(a: ZombieModel, b: ZombieModel) -> bool:
    """Can ``b``'s grenade animation draw ``a``'s grenade mesh untorn: the
    same grenade bones, placed alike relative to the grenade's root."""
    ra, rb = _grenade_rig(a), _grenade_rig(b)
    if set(ra) != set(rb):
        return False
    for name, xa in ra.items():
        xb = rb[name]
        ta, tb = xa.translation, xb.translation
        if max(abs(ta.x - tb.x), abs(ta.y - tb.y), abs(ta.z - tb.z)) > RIG_TOLERANCE:
            return False
        if max(abs(x - y) for row_a, row_b in zip(xa.rotation, xb.rotation, strict=True)
               for x, y in zip(row_a, row_b, strict=True)) > RIG_TOLERANCE:
            return False
    return True


def _choose_grenade(carriers: list[ZombieModel],
                    grenade_texture: str | None) -> ZombieModel:
    """The grenade everyone shares: the first with the requested texture,
    else the most common grenade geometry."""
    if grenade_texture is not None:
        chosen = next((zm for zm in carriers if any(
            t.material.lower() == grenade_texture.lower()
            for t in zm.grenade.triangles)), None)
        if chosen is not None:
            return chosen
    return max(carriers, key=lambda c: sum(
        _same_mesh(c.grenade, other.grenade, {}, {}) for other in carriers))


def unify_grenades(models: list[ZombieModel], canonical: ZombieModel,
                   report: ZhandsReport | None = None) -> list[str]:
    """One frog for everyone: every grenade model of ``models`` drops its
    own frog and, on another rig than ``canonical``'s, has its grenade
    animation redrawn for ``canonical``'s frog (:mod:`.frog`) — the merge
    then draws that one frog for all of them. A frog that cannot be paired
    (another UV layout) keeps its own. Returns the models redrawn."""
    from valve_qc_merger.merge_zhands.frog import redraw_grenade
    redrawn: list[str] = []
    scales: list[float] = []
    for zm in models:
        if zm.grenade is None:
            continue
        if not _same_grenade_rig(canonical, zm):
            done = redraw_grenade(canonical.grenade, zm.grenade, zm.model.anims,
                                  GRENADE_PREFIX)
            if done is None:
                if report is not None:
                    report.warnings.append(
                        f"{zm.stem}: its frog does not pair with {canonical.stem}'s "
                        "(another UV layout): kept with its own grenade")
                continue
            scales.append(done.scale)
            redrawn.append(zm.stem)
        zm.redrawn_by = canonical.stem
        zm.grenade = None
    if report is not None and scales:
        report.warnings.append(
            f"{len(scales)} grenade(s) redrawn for {canonical.stem}'s frog (their frogs "
            f"x{min(scales):.2f}-x{max(scales):.2f} its size)")
    return redrawn


def frog_mesh(canonical: ZombieModel, table: Smd) -> Smd:
    """``canonical``'s frog on ``table``'s nodes and bind: each vertex keeps
    its place in its bone's frame, so the game draws it exactly where the
    (redrawn) animation puts that bone, whatever the bind."""
    from valve_qc_merger.models.smd import Vertex
    source = canonical.grenade
    source_worlds = fk_worlds(source, source.frames[0])
    table_worlds = fk_worlds(table, table.frames[0])
    source_name = {n.index: n.name for n in source.nodes}
    table_index = {n.name: n.index for n in table.nodes}
    triangles = []
    for tri in source.triangles:
        vertices = []
        for v in tri.vertices:
            bone = source_name[v.bone]
            local = source_worlds[v.bone].inverse()
            world = table_worlds[table_index[bone]]
            vertices.append(Vertex(
                table_index[bone],
                world.transform_point(local.transform_point(v.position)),
                world.rotate_vector(local.rotate_vector(v.normal)), v.uv))
        triangles.append(dataclasses.replace(tri, vertices=tuple(vertices)))
    return Smd(nodes=list(table.nodes), frames=[table.frames[0]], triangles=triangles)


def canonical_grenade(models_in: list[ModelInput], grenade_prefix: str,
                      grenade_texture: str | None) -> ModelInput | None:
    """The model whose frog everyone gets (one frog for all, every part): of
    the frog rig most models share."""
    carriers = [zm for zm in (prepare_model(m.clone(), grenade_prefix) for m in models_in)
                if zm.grenade is not None]
    if not carriers:
        return None
    # the frog most zombies already have (so most look as they did: the CSO
    # bombs are x0.40 of aksha's on 99 of 118), its grenade_texture model first
    rigs: list[list[ZombieModel]] = []
    for zm in carriers:
        home = next((r for r in rigs if _same_grenade_rig(r[0], zm)), None)
        if home is None:
            rigs.append([zm])
        else:
            home.append(zm)
    common = max(rigs, key=len)
    chosen = _choose_grenade(common, grenade_texture)
    return next(m for m in models_in if m.name == chosen.stem)


def _skin_label(path: Path) -> str:
    """A grenade skin's name in entries and the manifest (its file stem)."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", path.stem).strip("_").lower() or "skin"


def _hands_label(members: list[ZombieModel]) -> str:
    zombies = {m.zombie for m in members}
    roles = {m.role for m in members}
    variants = {m.variant for m in members}
    if len(zombies) == 1 and roles == {"knife", "grenade"} and variants == {None}:
        return members[0].zombie
    return members[0].tag


def _unique_names(models: list[ZombieModel]) -> dict[tuple[str, str], str]:
    taken: set[str] = set()
    out: dict[tuple[str, str], str] = {}
    for zm in models:
        for seq in zm.model.anims:
            base = f"{zm.tag}_{seq}"
            final = base[:NAME_LIMIT]
            counter = 2
            while final.lower() in taken:
                suffix = f"_{counter}"
                final = base[:NAME_LIMIT - len(suffix)] + suffix
                counter += 1
            taken.add(final.lower())
            out[(zm.stem, seq)] = final
    return out


VERTEX_LIMIT = 2048  # studiomdl: vertices, and normals, per submodel
def hands_limit() -> int:
    """Models a part: the hands group (a leading blank + an entry each) and
    the grenade's 2 within the compiler's submodels, and pev_body
    (grenade + 2 x (hands + 1)) within a view model's byte."""
    return min(limits.submodels() - 3, limits.VIEW_BODY_VALUES // 2 - 1)


def hands_size(model: ModelInput, grenade_prefix: str = "frogbomb") -> tuple[int, int]:
    """(vertices, normals) of the one hands entry ``model`` becomes, counted
    as studiomdl does (the model is left untouched)."""
    zm = prepare_model(model.clone(), grenade_prefix)
    triangles = zm.hands.triangles if zm.hands is not None else []
    return (len({(v.bone, v.position) for t in triangles for v in t.vertices}),
            len({(v.bone, v.normal, t.material.lower()) for t in triangles
                 for v in t.vertices}))


def pool_bones(models: list[ZombieModel], report: ZhandsReport) -> int:
    """Share bone slots between the zombies' rigs (merge-v's bone pool): only
    one zombie's hands draw at a time and every sequence is one zombie's, so a
    rig whose names match no other (claws rarely have the 4+ fingers the hand
    matcher looks for) still lands on the slots of the rigs before it, by
    structure — parent under parent, largest rig first. The grenade's
    ``gren_*`` bones keep their names (one grenade for all). Reparents are
    exact per frame. Returns the slots used."""
    from valve_qc_merger.merge_view.bonepool import apply_pool, plan_pool
    tables: dict[str, dict[str, str | None]] = {}
    shared: set[str] = set()
    for zm in models:
        tables[zm.stem], own = _pool_table(zm)
        shared |= own
    plan = plan_pool(tables, shared, max_slots=BONE_LIMIT - len(shared))
    for zm in models:
        assignment = plan.assignments[zm.stem]
        apply_pool(zm.model, assignment, plan.slot_parent, root="")
        zm.renames = {orig: assignment.get(cur, cur) for orig, cur in zm.renames.items()}
        for bone, slot in assignment.items():
            if bone not in zm.renames.values():
                zm.renames.setdefault(bone, slot)
        zm.hands = zm.model.meshes["hands"]
        zm.grenade = zm.model.meshes.get("grenade")
    report.warnings.append(f"bones pooled: {plan.size} shared slots for "
                           f"{len(models)} model(s)")
    return plan.size


def zombie_of(name: str) -> str:
    """The zombie a ``v_<zombie>_<knife|grenade>[_variant]`` model belongs to."""
    match = _NAME_RE.match(name)
    return match.group("zombie").lower() if match else name.lower()


def _prepared(models_in: list[ModelInput], grenade_prefix: str,
              frog: ModelInput | None = None) -> list[ZombieModel]:
    """Copies of ``models_in`` split, renamed (grenades redrawn for ``frog``'s
    frog: one frog for all) and canonicalised as the merge does, reduced to
    their hands and grenade meshes (the originals stay)."""
    models = [prepare_model(m.clone(), grenade_prefix) for m in models_in]
    if frog is not None:
        unify_grenades(models, prepare_model(frog.clone(), grenade_prefix))
    canonicalize_hands(models, ZhandsReport())
    for zm in models:
        zm.model.meshes = {"hands": zm.hands}
        if zm.grenade is not None:
            zm.model.meshes["grenade"] = zm.grenade
    return models


def _pool_table(zm: ZombieModel) -> tuple[dict[str, str | None], set[str]]:
    """A model's bones for the pool (name -> parent name) and its grenade
    bones (shared by name, never pooled)."""
    fullest = max(zm.model.meshes.values(), key=lambda m: len(m.nodes))
    name_of = {n.index: n.name for n in fullest.nodes}
    table = {n.name: name_of.get(n.parent) for n in fullest.nodes}
    shared = {n.name for smd in zm.model.meshes.values() for n in smd.nodes
              if n.name.startswith(GRENADE_PREFIX)}
    return table, shared


def planned(models_in: list[ModelInput], grenade_prefix: str = "frogbomb",
            frog: ModelInput | None = None) -> tuple[int, int]:
    """(bones, largest sequence's anim bytes) a merge of ``models_in`` would
    have, with the bones pooled (nothing written; the models are left
    untouched). Pooling reshapes channels, so the bytes are measured with
    studiomdl's exact quantise+RLE replica."""
    models = _prepared(models_in, grenade_prefix, frog)
    pool_bones(models, ZhandsReport())
    inputs = [zm.model for zm in models]
    skeleton = merged_skeleton(inputs)
    if len(skeleton) > BONE_LIMIT:
        return len(skeleton), 0
    unify_skeletons(inputs, skeleton)
    return len(skeleton), max(sequence_sizes(inputs).values(), default=0)


class _PoolBones:
    """Bones a run of models would pool to without reshaping a rig — the
    merge's own pool plan, run on the bone tables alone (a fraction of
    :func:`planned`'s cost, which also applies the pool and measures every
    sequence)."""

    def __init__(self, models_in: list[ModelInput], grenade_prefix: str,
                 frog: ModelInput | None = None, skins: int = 0) -> None:
        self.tables: dict[str, dict[str, str | None]] = {}
        self.shared: dict[str, set[str]] = {}
        self.rig: dict[str, int] = {}  # model -> its grenade frog (-1: none)
        self.skins = skins
        rigs: list[ZombieModel] = []
        for model_in, zm in zip(models_in, _prepared(models_in, grenade_prefix, frog),
                                strict=True):
            if zm.redrawn_by is not None:  # drawn with the shared frog
                self.tables[model_in.name], self.shared[model_in.name] = _pool_table(zm)
                self.rig[model_in.name] = -2
                continue
            self.tables[model_in.name], self.shared[model_in.name] = _pool_table(zm)
            if zm.grenade is None:
                self.rig[model_in.name] = -1
                continue
            home = next((i for i, r in enumerate(rigs) if _same_grenade_rig(r, zm)), None)
            if home is None:
                rigs.append(zm)
                home = len(rigs) - 1
            self.rig[model_in.name] = home

    def body_fits(self, models_in: list[ModelInput]) -> bool:
        """pev_body (grenade entries + 1) x (hands + 1) within a view model's
        byte; entries = frogs x (default + skins)."""
        rigs = {self.rig[m.name] for m in models_in} - {-1}
        frogs = len(rigs - {-2}) + (1 if -2 in rigs else 0)  # -2: the shared frog
        grenades = frogs * (1 + self.skins)
        return (grenades + 1) * (len(models_in) + 1) <= limits.VIEW_BODY_VALUES

    def __call__(self, models_in: list[ModelInput]) -> int:
        from valve_qc_merger.merge_view.bonepool import plan_pool
        names = [m.name for m in models_in]
        shared = set().union(*(self.shared[n] for n in names))
        # without reshaping: a full pool reshapes the rigs instead of growing
        # (never over the limit, so no bound at all), and reshapes turn
        # constant channels into animated ones — the 64K a sequence goes first
        plan = plan_pool({n: self.tables[n] for n in names}, shared,
                         max_slots=BONE_LIMIT - len(shared), allow_reparent=False)
        return plan.size + len(shared)


def planned_bones(models_in: list[ModelInput], grenade_prefix: str = "frogbomb") -> int:
    """Bones a merge of ``models_in`` would have (see :func:`planned`)."""
    return planned(models_in, grenade_prefix)[0]


def _fits(plan: tuple[int, int], limit: int) -> bool:
    bones, worst = plan
    return bones <= limit and worst <= SEQ_DATA_LIMIT


def split_zombies(models: list[ModelInput], grenade_prefix: str = "frogbomb",
                  limit: int = BONE_LIMIT,
                  log: Callable[[str], None] | None = None,
                  progress: Callable[[int, int], None] | None = None,
                  *, frog: ModelInput | None = None,
                  skins: int = 0) -> list[list[ModelInput]]:
    """Parts under the bone limit (with the bones pooled), the 64K a sequence
    and the submodel limit, a zombie's models (knife, grenade, variants)
    always together, in input order.

    A part takes the longest run of zombies that fits: its end is found by
    the pooled bone count without reshapes (cheap: :class:`_PoolBones`),
    galloping then halving; the run is then measured exactly
    (:func:`planned`) — halved while a sequence is over 64K, else grown by a
    few zombies while it fits — a few exact runs a part, not one after every
    zombie (quadratic: most of an hour on 241 models). ``log`` hears each
    exact check, ``progress`` (zombies placed, zombies) each part."""
    log = log or (lambda _message: None)
    zombies: dict[str, list[ModelInput]] = {}
    for model in models:
        zombies.setdefault(zombie_of(model.name), []).append(model)
    groups = list(zombies.values())
    pool_bones_of = _PoolBones(models, grenade_prefix, frog, skins)

    def run(start: int, end: int) -> list[ModelInput]:
        return [model for group in groups[start:end] for model in group]

    def fits_bones(start: int, end: int) -> bool:
        trial = run(start, end)
        # one hands entry per model at most, plus the grenade group's two
        return (len(trial) <= hands_limit() and pool_bones_of.body_fits(trial)
                and pool_bones_of(trial) <= limit)

    def fits(start: int, end: int) -> bool:
        trial = run(start, end)
        bones, worst = planned(trial, grenade_prefix, frog)
        verdict = _fits((bones, worst), limit)
        log(f"    part {len(parts) + 1}: {len(trial)} model(s) "
            f"({', '.join(zombie_of(g[0].name) for g in groups[start:end])}) — "
            f"{bones} bones, largest sequence {worst // 1024} KB: "
            f"{'fits' if verdict else 'too big'}")
        return verdict

    def longest(start: int, good: int, end: int, test) -> int:
        """The last end in (good, end] that passes ``test`` (good passes or
        is the one zombie a part always takes); gallop from good, then halve."""
        bad, step = None, 1
        while good < end:
            trial = min(good + step, end)
            if test(start, trial):
                good, step = trial, step * 2
            else:
                bad = trial
                break
        while bad is not None and bad - good > 1:
            middle = (good + bad) // 2
            if test(start, middle):
                good = middle
            else:
                bad = middle
        return good

    parts: list[list[ModelInput]] = []
    start = 0
    while start < len(groups):
        # a part takes its first zombie whatever; the longest run pooling
        # without a reshape, checked exactly, then grown while it fits
        # exactly (the merge may still reshape a little within the 64K)
        end = longest(start, start + 1, len(groups), fits_bones)
        if end > start + 1 and not fits(start, end):
            end = longest(start, start + 1, end - 1, fits)
        else:
            end = longest(start, end, min(len(groups), end + 4), fits)
        parts.append(run(start, end))
        start = end
        if progress is not None:
            progress(start, len(groups))
    return parts


def merge_zhands(
    models_in: list[ModelInput],
    out_dir: Path,
    name: str,
    *,
    grenade_prefix: str = "frogbomb",
    grenade_texture: str | None = None,
    manifest_format: str = "ini",
    frog: ModelInput | None = None,
    grenade_skins: list[str] | None = None,
) -> ZhandsReport:
    """Merge zombie hand models into ``out_dir/<name>.qc``; returns a report.
    ``frog``: the model whose frog every grenade gets (its animation redrawn
    for it; :func:`canonical_grenade`) — None: a frog a grenade rig;
    ``grenade_skins``: BMPs, the frog in each of them (more grenade entries,
    ``pev_body_<skin>`` in the manifest)."""
    report = ZhandsReport()
    originals = {
        m.name: {s.name: str(_seq_path(m, s.name)) for s in m.sequences
                 if s.name in m.anims}
        for m in models_in
    }
    # the frog first: it may be one of models_in (one object after pickling),
    # which prepare_model renames in place
    shared_frog = prepare_model(frog.clone(), grenade_prefix) if frog is not None else None
    models = [prepare_model(m, grenade_prefix) for m in models_in]
    if shared_frog is not None and shared_frog.grenade is not None:
        unify_grenades(models, shared_frog, report)
    else:
        shared_frog = None
    reference = canonicalize_hands(models, report)
    for zm in models:
        if "$attachment" in zm.model.qc_text:
            report.warnings.append(
                f"{zm.stem}: $attachment lines ignored (zombie hands carry none)")

    # One node table for everything.
    for zm in models:
        zm.model.meshes = {"hands": zm.hands}
        if zm.grenade is not None:
            zm.model.meshes["grenade"] = zm.grenade
    pool_bones(models, report)
    inputs = [zm.model for zm in models]
    skeleton = merged_skeleton(inputs)
    report.bones = len(skeleton)
    if report.bones > BONE_LIMIT:
        raise ZhandsError(
            f"merged skeleton has {report.bones} bones (limit {BONE_LIMIT}); "
            f"reference rig {reference}"
        )
    unify_skeletons(inputs, skeleton)
    for zm in models:
        zm.hands = zm.model.meshes["hands"]
        zm.grenade = zm.model.meshes.get("grenade")
    for (model_name, seq_name), size in sequence_sizes(inputs).items():
        if size > SEQ_DATA_LIMIT:
            report.warnings.append(
                f"{model_name}: sequence {seq_name!r} anim data is {size} bytes "
                "(studiomdl caps one sequence at 64K)")

    out_dir.mkdir(parents=True, exist_ok=True)
    stage = _TextureStage(out_dir, report)

    # Hands: one entry per distinct (geometry, render mode) mesh.
    groups: list[list[ZombieModel]] = []
    for zm in models:
        assert zm.hands is not None
        for members in groups:
            owner = members[0]
            if _same_mesh(owner.hands, zm.hands, owner.render_modes,
                          zm.render_modes):
                members.append(zm)
                break
        else:
            groups.append([zm])
    hand_entries: list[tuple[str, Smd]] = []
    hand_index: dict[str, int] = {}
    labels: set[str] = set()
    (out_dir / "hands").mkdir(exist_ok=True)
    for members in groups:
        owner = members[0]
        label = _hands_label(members)
        while label in labels:
            label += "_"
        labels.add(label)
        mesh = _restage(owner.hands, owner, stage)
        (out_dir / "hands" / f"{label}.smd").write_text(
            write_smd_text(mesh), encoding="latin-1")
        for zm in members:
            hand_index[zm.stem] = len(hand_entries)
        hand_entries.append((label, mesh))
    report.hands = [label for label, _ in hand_entries]

    # Grenades: one mesh for every model with the same grenade RIG (the
    # requested texture, else the most common geometry among them); with
    # one frog for all (unify_grenades) the redrawn ones use the chosen's.
    # A frog of another rig drawn by another rig's animation is torn apart,
    # so each rig left is one more frog. Every frog comes in its default
    # texture and in each grenade skin: one entry each.
    carriers = [zm for zm in models if zm.grenade is not None]
    variants: list[list[ZombieModel]] = []
    for zm in carriers:
        home = next((v for v in variants if _same_grenade_rig(v[0], zm)), None)
        if home is None:
            variants.append([zm])
        else:
            home.append(zm)
    grenade_paths: list[str] = []
    grenade_index: dict[str, int] = {}  # model -> its frog's default entry (1…)
    skin_index: dict[str, dict[str, int]] = {}  # model -> skin label -> entry
    chosen_from: list[str] = []
    skins = [(_skin_label(Path(path)), Path(path)) for path in (grenade_skins or [])]
    shared_users = [zm for zm in models if zm.redrawn_by is not None]
    frogs: list[tuple[ZombieModel, Smd, list[ZombieModel]]] = []
    if shared_users:  # one frog for all: drawn on this part's node table
        mesh = frog_mesh(shared_frog, models[0].hands)
        frogs.append((shared_frog, mesh, shared_users))
    for members in variants:
        frogs.append((_choose_grenade(members, grenade_texture), None, members))
    if frogs:
        (out_dir / "grenade").mkdir(exist_ok=True)
    for number, (chosen, built, members) in enumerate(frogs, 1):
        chosen_from.append(chosen.stem)
        mesh = _restage(built if built is not None else chosen.grenade, chosen, stage)
        stem = "grenade" if number == 1 else f"grenade{number}"
        (out_dir / "grenade" / f"{stem}.smd").write_text(
            write_smd_text(mesh), encoding="latin-1")
        grenade_paths.append(f"grenade/{stem}")
        default = len(grenade_paths)
        own_skins: dict[str, int] = {}
        main = Counter(t.material for t in mesh.triangles).most_common(1)[0][0]
        for label, path in skins:
            texture = stage.stage(path.parent, path.name, None)
            skinned = Smd(nodes=list(mesh.nodes), frames=list(mesh.frames), triangles=[
                dataclasses.replace(t, material=texture) if t.material == main else t
                for t in mesh.triangles])
            (out_dir / "grenade" / f"{stem}_{label}.smd").write_text(
                write_smd_text(skinned), encoding="latin-1")
            grenade_paths.append(f"grenade/{stem}_{label}")
            own_skins[label] = len(grenade_paths)
        for zm in members:
            grenade_index[zm.stem] = default
            skin_index[zm.stem] = own_skins
            if built is None and not _same_mesh(chosen.grenade, zm.grenade, {}, {}):
                report.warnings.append(
                    f"{zm.stem}: its own grenade mesh is replaced by "
                    f"{chosen.stem}'s (the same grenade rig)")
    if grenade_texture is not None and carriers and not any(
            t.material.lower() == grenade_texture.lower()
            for zm in carriers for t in zm.grenade.triangles):
        report.warnings.append(f"no grenade uses texture {grenade_texture!r}; "
                               "using the most common grenade of each rig")
    if len(frogs) > 1:
        report.warnings.append(f"{len(frogs)} frogs: one grenade entry each")
    report.grenade_from = ", ".join(chosen_from) if chosen_from else None
    report.textures = len(stage.taken)

    # Sequences (deduped by content + fps + events + loop).
    seq_names = _unique_names(models)
    order: list[tuple[str, str, object]] = []
    seen: dict[tuple, int] = {}
    manifest_anim: dict[str, dict[str, int]] = {zm.stem: {} for zm in models}
    for zm in models:
        (out_dir / zm.stem).mkdir(exist_ok=True)
        for seq_name, anim in zm.model.anims.items():
            meta = next((s for s in zm.model.sequences if s.name == seq_name), None)
            text = write_smd_text(anim)
            key = (hashlib.md5(text.encode("latin-1")).hexdigest(),
                   meta.fps if meta else None, meta.events if meta else (),
                   meta.loop if meta else False)
            index = seen.get(key)
            if index is None:
                final = seq_names[(zm.stem, seq_name)]
                (out_dir / zm.stem / f"{final}.smd").write_text(text, encoding="latin-1")
                index = len(order)
                order.append((final, f"{zm.stem}/{final}", meta))
                seen[key] = index
            else:
                report.sequences_deduped += 1
            manifest_anim[zm.stem][seq_name] = index
    report.sequences = len(order)

    # Budgets.
    grenades = len(grenade_paths)
    submodels = 1 + len(hand_entries) + (1 + grenades if grenades else 0)  # blanks lead
    if submodels > limits.submodels():
        raise ZhandsError(f"{submodels} submodels exceed studiomdl's {limits.submodels()}")
    if report.sequences > SEQUENCE_LIMIT:
        report.warnings.append(
            f"{report.sequences} sequences (> {SEQUENCE_LIMIT}: the game "
            "addresses viewmodel animations by a byte)")
    if report.textures > TEXTURE_LIMIT:
        report.warnings.append(f"{report.textures} textures (> {TEXTURE_LIMIT})")

    # The grenade group (blank + one entry a grenade rig) comes FIRST, so it
    # is the low digit: pev_body = grenade + (grenades + 1) * (hands + 1) —
    # with one rig grenade_on + 2 * (hands + 1): a zombie's knife and grenade
    # are n and n | 1, the same convention as merge-v --shared-hands.
    grenade_stride = grenades + 1
    if grenade_stride * (len(hand_entries) + 1) > limits.VIEW_BODY_VALUES:
        raise ZhandsError(
            f"{grenades} grenade rigs x {len(hand_entries)} hands need pev_body up to "
            f"{grenade_stride * (len(hand_entries) + 1) - 1} (a view model's body is a byte)")
    for zm in models:
        grenade = grenade_index.get(zm.stem, 0) if zm.role == "grenade" else 0
        # +1: the hands group leads with a blank (pev_body 0 draws nothing:
        # the client prediction shows nothing until the plugin sets the body)
        report.pev_body[zm.stem] = grenade + grenade_stride * (hand_index[zm.stem] + 1)

    lines = [
        "// Generated by valve-qc-merger merge-zhands: zombie hands + one",
        "// grenade entries (frog x skin); pev_body = grenade + (grenades + 1) * (hands + 1);",
        "// 0 draws nothing.",
        "",
        f'$modelname "{name}.mdl"',
        '$cd "."',
        '$cdtexture "."',
        "$cliptotextures",
        "$scale 1.0",
        "",
    ]
    for texture, mode in sorted(stage.modes.items()):
        lines.append(f'$texrendermode "{texture}" {mode}')
    if stage.modes:
        lines.append("")
    if grenade_paths:
        lines += ['$bodygroup "grenade"', "{", "\tblank",
                  *(f'\tstudio "{path}"' for path in grenade_paths), "}"]
    lines += ['$bodygroup "hands"', "{", "\tblank"]
    lines += [f'\tstudio "hands/{label}"' for label, _ in hand_entries]
    lines += ["}"]
    lines += ["", "$flags 0", ""]
    for final, smd_path, meta in order:
        lines.append(f'$sequence "{final}" {{')
        lines.append(f'\t"{smd_path}"')
        if meta is not None:
            for event in meta.events:
                lines.append(f"\t{event}")
            if meta.fps is not None:
                fps = int(meta.fps) if meta.fps == int(meta.fps) else meta.fps
                lines.append(f"\tfps {fps}")
            if meta.loop:
                lines.append("\tloop")
        lines.append("}")
    (out_dir / f"{name}.qc").write_text("\n".join(lines) + "\n", encoding="latin-1")

    report.manifest = {
        zm.stem: {
            "pev_body": report.pev_body[zm.stem],
            # the same grenade in each skin (a grenade model only)
            **({f"pev_body_{label}": entry + grenade_stride * (hand_index[zm.stem] + 1)
                for label, entry in skin_index.get(zm.stem, {}).items()}
               if zm.role == "grenade" else {}),
            "hands": hand_entries[hand_index[zm.stem]][0],
            **{f"anim_{k}": v for k, v in manifest_anim[zm.stem].items()},
        }
        for zm in models
    }
    write_manifest_data(out_dir, report.manifest, manifest_format)
    report.gate = verify(out_dir, models, originals, order, manifest_anim)
    return report


def _seq_path(model: ModelInput, seq_name: str) -> Path:
    meta = next(s for s in model.sequences if s.name == seq_name)
    relative = (meta.smd or "").replace("\\", "/")
    if not relative.lower().endswith(".smd"):
        relative += ".smd"
    return model.directory / relative


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #
def verify(
    out_dir: Path,
    models: list[ZombieModel],
    originals: dict[str, dict[str, str]],
    order: list[tuple[str, str, object]],
    manifest_anim: dict[str, dict[str, int]],
) -> list[tuple[str, bool, str]]:
    """Re-prove from the emitted files: one node table, every mapped bone's
    world position per frame (first/mid/last) matches the original anim."""
    rows: list[tuple[str, bool, str]] = []
    emitted = {path: parse_smd_file(out_dir / f"{path}.smd") for _f, path, _m in order}
    meshes = [parse_smd_file(p) for p in sorted(out_dir.glob("*/*.smd"))
              if p.parent.name in ("hands", "grenade")]
    tables = {tuple((n.index, n.name, n.parent) for n in s.nodes)
              for s in [*emitted.values(), *meshes]}
    rows.append(("tables_consistent", len(tables) == 1,
                 f"{len(emitted) + len(meshes)} SMDs, {len(tables)} node table(s)"))

    worst, worst_at, checked = 0.0, "", 0
    for zm in models:
        for seq_name, index in manifest_anim[zm.stem].items():
            source = originals.get(zm.stem, {}).get(seq_name)
            if source is None:
                continue
            original = parse_smd_file(Path(source))
            merged = emitted[order[index][1]]
            if len(original.frames) != len(merged.frames):
                rows.append(("pose_preserved", False,
                             f"{zm.stem}/{seq_name}: frame count changed"))
                return rows
            merged_index = {n.name: n.index for n in merged.nodes}
            original_names = {n.index: n.name for n in original.nodes}
            for f in sorted({0, len(original.frames) // 2, len(original.frames) - 1}):
                ow = fk_worlds(original, original.frames[f])
                mw = fk_worlds(merged, merged.frames[f])
                for oi, world in ow.items():
                    final = zm.renames.get(original_names[oi], original_names[oi])
                    if final not in merged_index:
                        continue
                    if zm.redrawn_by is not None and final.startswith(GRENADE_PREFIX):
                        continue  # redrawn for the shared frog on purpose
                    got = mw[merged_index[final]].translation
                    want = world.translation
                    delta = max(abs(got.x - want.x), abs(got.y - want.y),
                                abs(got.z - want.z))
                    checked += 1
                    if delta > worst:
                        worst, worst_at = delta, f"{zm.stem}/{seq_name} f{f}"
    rows.append(("pose_preserved", worst <= POSE_EPSILON,
                 f"{checked} bone-frames, worst {worst:.2e}u ({worst_at})"))
    return rows


__all__ = ["ZhandsError", "ZhandsReport", "merge_zhands", "prepare_model"]
