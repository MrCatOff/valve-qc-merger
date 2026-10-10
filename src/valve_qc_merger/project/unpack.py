"""Unpack a merged model (a "pack") back into the models it was built from,
so imports take them as assets of their own instead of a model nothing can
merge again.

A merge names each submodel ``<source model>/<mesh>`` (merge-v / merge-p /
goldsource packs: ``v_awp_kraken/v_zgun``, ``geometry/p_deagle``), so each
source gets back its submodels from every bodygroup — plus the shared ones
(``hands/…`` of a shared-hands pack, unnamed parts). Its sequences:

- a label ``<source>__<label>`` is that source's (the prefix a merge adds on
  a clash, dropped again);
- the others follow the order the merge wrote them in, one source after the
  other: an ``idle*`` label (a view model's sequence 0) opens the block of the
  next source that has prefixed labels, any other continues the block;
- a player pack's sequences are the shared rig's: every source gets all;
- a source left with none gets the pack's first.

The skeleton is cut to what a source uses: the bones of its vertices,
attachments and hitboxes and their ancestors (the others are not ancestors
of any of them, so no kept bone moves). A merge drops a sequence identical to
an earlier model's, so a source's list can come back short: the asset's
notes say the sequences were inferred.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd, Triangle, Vertex
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.writers.smd import write_smd_file

_PLAYER_SEQUENCE = re.compile(r"(?i)^(?:ref_aim|crouch_aim|ref_shoot|crouch_shoot)")
_IDLE = re.compile(r"(?i)^idle")
_SEQUENCE = re.compile(r'^\$sequence\s+"([^"]*)"\s*\{\s*$')


@dataclass
class Unpacked:
    name: str
    folder: Path
    submodels: int
    sequences: int
    notes: list[str] = field(default_factory=list)


def _source_of(name: str) -> str | None:
    """The source model a submodel name names (None: shared or unnamed)."""
    parts = [c for c in name.replace("\\", "/").split("/") if c]
    if len(parts) < 2 or parts[0] in (".", ".."):
        return None
    if parts[0].lower() == "geometry":
        return parts[-1]
    if parts[0].lower() == "hands":
        return None
    return parts[0]


def _blocks(lines: list[str]) -> list[tuple[str, list[str]]]:
    """``(label, block lines)`` of every ``$sequence`` of a decompiled QC."""
    out: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(lines):
        match = _SEQUENCE.match(lines[i].strip())
        if match is None:
            i += 1
            continue
        j = i + 1
        while j < len(lines) and lines[j].strip() != "}":
            j += 1
        out.append((match.group(1), lines[i + 1:j]))
        i = j + 1
    return out


def assign_sequences(labels: list[str], sources: list[str],
                     player: bool = False) -> dict[str, list[tuple[int, str]]]:
    """``{source: [(pack sequence index, label without prefix)]}``."""
    out: dict[str, list[tuple[int, str]]] = {s: [] for s in sources}
    if player:
        for source in sources:
            out[source] = [(i, label) for i, label in enumerate(labels)]
        return out
    lowered = {s.lower(): s for s in sources}
    owners: list[str | None] = []
    for label in labels:
        head, sep, _rest = label.partition("__")
        owners.append(lowered.get(head.lower()) if sep else None)
    current = sources[0] if sources else None
    for i, label in enumerate(labels):
        owner = owners[i]
        if owner is None:
            if _IDLE.match(label):
                following = next((o for o in owners[i + 1:] if o is not None), None)
                if following is not None and following != current \
                        and not out.get(following) and current is not None \
                        and out.get(current):
                    current = following
            owner = current
        else:
            current = owner
        if owner is None:
            continue
        plain = label.split("__", 1)[1] if owners[i] is not None else label
        out[owner].append((i, plain))
    return out


def _prune(smd: Smd, keep: set[int]) -> Smd:
    """``smd`` with only ``keep`` bones (ancestor-closed), reindexed."""
    order = [n for n in smd.nodes if n.index in keep]
    new = {n.index: k for k, n in enumerate(order)}
    nodes = [Node(new[n.index], n.name, new.get(n.parent, -1)) for n in order]
    frames = [Frame(f.time, tuple(BonePose(new[p.bone], p.position, p.rotation)
                                  for p in f.poses if p.bone in new)) for f in smd.frames]
    triangles = [Triangle(t.material, tuple(Vertex(new[v.bone], v.position, v.normal, v.uv)
                                            for v in t.vertices))  # type: ignore[arg-type]
                 for t in smd.triangles]
    return Smd(nodes=nodes, frames=frames, triangles=triangles)


def unpack(pack: Path, out: Path, *, skip: set[str] | None = None) -> list[Unpacked]:
    """Write every source of ``pack`` (but those in ``skip``, lower case)
    as a decompiled model folder ``out/<source>/``."""
    from valve_qc_merger.mdl.decompile import _file_names
    from valve_qc_merger.mdl.reader import read_mdl
    from valve_qc_merger.services.base import CollectingReporter
    from valve_qc_merger.services.decompile import DecompileOptions, run_decompile
    skip = {s.lower() for s in (skip or set())}
    model = read_mdl(pack)
    staging = out / f".unpack_{pack.stem}"
    shutil.rmtree(staging, ignore_errors=True)
    run_decompile(DecompileOptions(source=pack, out=staging), CollectingReporter())
    decompiled = staging / pack.stem
    qc_lines = next(decompiled.glob("*.qc")).read_text(encoding="latin-1").splitlines()

    # every submodel: (bodypart, decompiled file stem or None for blank, source)
    raw = [sub.name for part in model.bodyparts for sub in part.models]
    stems = iter(_file_names([Path(n.replace("\\", "/")).stem or "studio" for n in raw]))
    entries: list[tuple[str, str | None, str | None]] = []
    for part in model.bodyparts:
        for sub in part.models:
            stem = next(stems)
            blank = not sub.meshes and not sub.vertices
            entries.append((part.name, None if blank else stem, _source_of(sub.name)))
    sources = list(dict.fromkeys(s for _p, _f, s in entries if s is not None))
    blocks = _blocks(qc_lines)
    labels = [label for label, _body in blocks]
    player = any(_PLAYER_SEQUENCE.match(label) for label in labels)
    owned = assign_sequences(labels, sources, player=player)
    header = [line for line in qc_lines
              if re.match(r"^\$(eyeposition|bbox|cbox|flags)\b", line)]
    render = {m.group(1).lower(): line for line in qc_lines
              if (m := re.match(r"^\$texrendermode\s+(\S+)", line))}
    attachments = [(line, m.group(1)) for line in qc_lines
                   if (m := re.match(r'^\$attachment\s+\d+\s+"([^"]+)"', line))]
    hitboxes = [(line, m.group(1)) for line in qc_lines
                if (m := re.match(r'^\$hbox\s+-?\d+\s+"([^"]+)"', line))]

    made: list[Unpacked] = []
    for source in sources:
        if source.lower() in skip:
            continue
        target = out / source
        shutil.rmtree(target, ignore_errors=True)
        (target / "anims").mkdir(parents=True)
        groups: dict[str, list[str]] = {}
        for part, stem, owner in entries:
            if stem is not None and owner in (source, None):
                groups.setdefault(part, []).append(stem)
        meshes = {stem: parse_smd_file(decompiled / f"{stem}.smd")
                  for stems_ in groups.values() for stem in stems_}
        if not meshes:
            continue
        nodes = next(iter(meshes.values())).nodes
        parent = {n.index: n.parent for n in nodes}
        index = {n.name: n.index for n in nodes}
        weighted: set[int] = {v.bone for smd in meshes.values()
                              for t in smd.triangles for v in t.vertices}
        # studiomdl keeps the bones of vertices and their ancestors only: an
        # attachment bone without vertices gets an invisible anchor triangle,
        # a hitbox on a bone that will not survive is left out
        anchored = {index[b] for _l, b in attachments if b in index} - weighted
        keep = weighted | anchored
        for bone in list(keep):
            while parent.get(bone, -1) >= 0:
                bone = parent[bone]
                keep.add(bone)
        kept_names = {n.name for n in nodes if n.index in keep}
        materials: set[str] = set()
        first = True
        for stem, smd in meshes.items():
            if first and anchored and smd.triangles:
                seed = smd.triangles[0]
                for bone in sorted(anchored):
                    pin = Vertex(bone, seed.vertices[0].position, seed.vertices[0].normal,
                                 seed.vertices[0].uv)
                    smd.triangles.append(Triangle(seed.material, (pin, pin, pin)))
                first = False
            pruned = _prune(smd, keep)
            materials |= {t.material for t in pruned.triangles}
            write_smd_file(pruned, target / f"{stem}.smd")
        (target / "maps_8bit").mkdir()
        for material in sorted(materials):
            texture = decompiled / "maps_8bit" / material
            if texture.is_file():
                shutil.copy2(texture, target / "maps_8bit" / material)

        notes: list[str] = []
        sequences = owned.get(source) or ([(0, labels[0])] if labels else [])
        if not owned.get(source) and labels:
            notes.append("no sequence of its own in the pack: the pack's first one")
        seq_lines: list[str] = []
        taken: set[str] = set()
        for position, label in sequences:
            body = blocks[position][1]
            name = label
            counter = 2
            while name.lower() in taken:
                name, counter = f"{label}_{counter}", counter + 1
            taken.add(name.lower())
            new_body = []
            for line in body:
                match = re.match(r'^(\s*)"\./anims/([^"]+)"\s*$', line)
                if match:
                    anim = parse_smd_file(decompiled / "anims" / f"{match.group(2)}.smd")
                    file_name = f"{name}" if len(new_body) == 0 else f"{name}_{len(new_body)}"
                    write_smd_file(_prune(anim, keep), target / "anims" / f"{file_name}.smd")
                    new_body.append(f'{match.group(1)}"./anims/{file_name}"')
                else:
                    new_body.append(line)
            seq_lines += [f'$sequence "{name}" {{', *new_body, "}"]
        if not player and sources.index(source) > 0:
            notes.append(f"unpacked from {pack.name}: sequences inferred from the pack "
                         "(a merge drops one identical to an earlier model's) — check them")

        lines = [f"// unpacked by valve-qc-merger from {pack.name}", "",
                 f"$modelname {source}.mdl", "$cd .", "$cdtexture ./maps_8bit",
                 "$cliptotextures", *header, ""]
        lines += [render[m.lower()] for m in sorted(materials) if m.lower() in render]
        for part, stems_ in groups.items():
            if len(stems_) == 1:
                lines.append(f'$body "{part}" "{stems_[0]}"')
            else:
                lines += [f'$bodygroup "{part}"', "{",
                          *[f'    studio "{s}"' for s in stems_], "}"]
        lines.append("")
        lines += [line for line, bone in attachments if bone in kept_names]
        lines += [line for line, bone in hitboxes if bone in kept_names]
        lines += ["", *seq_lines]
        (target / f"{source}.qc").write_text("\n".join(lines) + "\n", encoding="latin-1")
        made.append(Unpacked(source, target, len(meshes), len(sequences), notes))
    shutil.rmtree(staging, ignore_errors=True)
    return made


__all__ = ["Unpacked", "assign_sequences", "unpack"]
