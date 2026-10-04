"""Decompile a GoldSource ``.mdl`` into a QC + SMD + BMP folder, in process.

The layout matches what the rest of the toolkit (and tools/decompmdl) uses::

    <out>/<model>/
      <model>.qc
      <submodel>.smd          one reference SMD per submodel, bind-pose positions
      anims/<sequence>.smd    one per sequence (``<sequence>_blendN`` for blends)
      maps_8bit/<texture>.bmp

Vertices are stored bone-local in the .mdl; they are moved to the bind pose
(the bones' default values) as studiomdl expects in a reference SMD. Texture
names that are not printable ASCII (CSO's CP949 names) are renamed to ASCII.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.mdl.reader import (
    MOTION_FLAGS,
    NF_ADDITIVE,
    NF_FLATSHADE,
    NF_FULLBRIGHT,
    NF_MASKED,
    STUDIO_LOOPING,
    STUDIO_RLOOP,
    StudioModel,
    read_mdl,
)
from valve_qc_merger.merge_view.bmp8 import Bmp8, write_bmp8
from valve_qc_merger.models.geometry import Vector2, Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd, Triangle, Vertex
from valve_qc_merger.transform import Transform
from valve_qc_merger.writers.smd import write_smd_text

# HLSDK activity map (studiomdl's activity_map, activity.h order).
ACTIVITIES = [
    "ACT_RESET", "ACT_IDLE", "ACT_GUARD", "ACT_WALK", "ACT_RUN", "ACT_FLY",
    "ACT_SWIM", "ACT_HOP", "ACT_LEAP", "ACT_FALL", "ACT_LAND", "ACT_STRAFE_LEFT",
    "ACT_STRAFE_RIGHT", "ACT_ROLL_LEFT", "ACT_ROLL_RIGHT", "ACT_TURN_LEFT",
    "ACT_TURN_RIGHT", "ACT_CROUCH", "ACT_CROUCHIDLE", "ACT_STAND", "ACT_USE",
    "ACT_SIGNAL1", "ACT_SIGNAL2", "ACT_SIGNAL3", "ACT_TWITCH", "ACT_COWER",
    "ACT_SMALL_FLINCH", "ACT_BIG_FLINCH", "ACT_RANGE_ATTACK1", "ACT_RANGE_ATTACK2",
    "ACT_MELEE_ATTACK1", "ACT_MELEE_ATTACK2", "ACT_RELOAD", "ACT_ARM",
    "ACT_DISARM", "ACT_EAT", "ACT_DIESIMPLE", "ACT_DIEBACKWARD", "ACT_DIEFORWARD",
    "ACT_DIEVIOLENT", "ACT_BARNACLE_HIT", "ACT_BARNACLE_PULL",
    "ACT_BARNACLE_CHOMP", "ACT_BARNACLE_CHEW", "ACT_SLEEP", "ACT_INSPECT_FLOOR",
    "ACT_INSPECT_WALL", "ACT_IDLE_ANGRY", "ACT_WALK_HURT", "ACT_RUN_HURT",
    "ACT_HOVER", "ACT_GLIDE", "ACT_FLY_LEFT", "ACT_FLY_RIGHT", "ACT_DETECT_SCENT",
    "ACT_SNIFF", "ACT_BITE", "ACT_THREAT_DISPLAY", "ACT_FEAR_DISPLAY",
    "ACT_EXCITED", "ACT_SPECIAL_ATTACK1", "ACT_SPECIAL_ATTACK2", "ACT_COMBAT_IDLE",
    "ACT_WALK_SCARED", "ACT_RUN_SCARED", "ACT_VICTORY_DANCE", "ACT_DIE_HEADSHOT",
    "ACT_DIE_CHESTSHOT", "ACT_DIE_GUTSHOT", "ACT_DIE_BACKSHOT", "ACT_FLINCH_HEAD",
    "ACT_FLINCH_CHEST", "ACT_FLINCH_STOMACH", "ACT_FLINCH_LEFTARM",
    "ACT_FLINCH_RIGHTARM", "ACT_FLINCH_LEFTLEG", "ACT_FLINCH_RIGHTLEG",
]

_UNSAFE_FILE = re.compile(r'[\x20<>:"/\\|?*]')  # space and path characters
_NON_ASCII = re.compile(r"[^\x21-\x7e]")
NAME_STEM_LIMIT = 40  # leaves room for merge suffixes under studiomdl's 64


@dataclass
class DecompileResult:
    qc: Path
    directory: Path
    warnings: list[str] = field(default_factory=list)


def _unrotate(pose: tuple[float, ...]) -> tuple[float, ...]:
    """Undo studiomdl's +90 deg Z turn on a ROOT bone's ANIMATION pose.

    studiomdl turns the root bones of every animation SMD by 90 deg about Z
    (its default ``$origin`` rotation) while the bone defaults keep the
    reference skeleton as authored. So animation frames are turned back,
    ``(x, y) -> (y, -x)`` and ``rz -> rz - pi/2``, and the reference SMD
    keeps the stored defaults (as tools/decompmdl does); recompiling gives
    back the same model.
    """
    x, y, z, rx, ry, rz = pose
    return (y, -x, z, rx, ry, rz - math.pi / 2)


def _smd_pose(model: StudioModel, pose: list[tuple[float, ...]]) -> list[tuple[float, ...]]:
    return [_unrotate(p) if model.bones[i].parent < 0 else p for i, p in enumerate(pose)]


def _bind_worlds(model: StudioModel) -> list[Transform]:
    worlds: list[Transform] = []
    for bone in model.bones:
        value = bone.value
        worlds.append(Transform.from_pos_euler(Vector3(*value[:3]), Vector3(*value[3:])))
    for i, bone in enumerate(model.bones):
        if bone.parent >= 0:
            worlds[i] = worlds[bone.parent].compose(worlds[i])
    return worlds


def _nodes(model: StudioModel) -> list[Node]:
    return [Node(i, b.name, b.parent) for i, b in enumerate(model.bones)]


def _frame(time: int, pose: list[tuple[float, ...]]) -> Frame:
    return Frame(time, tuple(
        BonePose(i, Vector3(p[0], p[1], p[2]), Vector3(p[3], p[4], p[5]))
        for i, p in enumerate(pose)
    ))


def _file_names(raw: list[str], suffix: str = "") -> list[str]:
    """Collision-free, filesystem- and QC-safe names (spaces kept out)."""
    taken: set[str] = set()
    out = []
    for name in raw:
        stem = name[: -len(suffix)] if suffix and name.lower().endswith(suffix) else name
        # spaces and path characters become "_" (nothing is trimmed, so
        # "Luger_P_08_silver .bmp" stays distinct); non-ASCII bytes (CSO's
        # CP949 names) are dropped; the stem is capped for studiomdl.
        clean = _NON_ASCII.sub("", _UNSAFE_FILE.sub("_", stem)).strip(".")
        clean = clean[:NAME_STEM_LIMIT] or "unnamed"
        candidate, counter = clean, 2
        while candidate.lower() in taken:
            candidate = f"{clean}_{counter}"
            counter += 1
        taken.add(candidate.lower())
        out.append(candidate + suffix)
    return out


def _fmt(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _vec(values: tuple[float, ...]) -> str:
    return " ".join(_fmt(v) for v in values)


def decompile_mdl(mdl: Path, out_root: Path) -> DecompileResult:
    """Decompile ``mdl`` into ``out_root/<model stem>/``."""
    model = read_mdl(mdl)
    stem = Path(mdl).stem
    out = Path(out_root) / stem
    (out / "anims").mkdir(parents=True, exist_ok=True)
    (out / "maps_8bit").mkdir(exist_ok=True)
    warnings = list(model.warnings)
    worlds = _bind_worlds(model)
    nodes = _nodes(model)
    bind = _frame(0, [b.value for b in model.bones])

    # -- textures ----------------------------------------------------------
    tex_names = _file_names([t.name for t in model.textures], ".bmp")
    for texture, file_name in zip(model.textures, tex_names, strict=True):
        # a real rename (too long, non-ASCII, unsafe characters) is worth a
        # warning; only adding the missing ".bmp" (version 9 names) is not
        if file_name.lower() not in (texture.name.lower(), f"{texture.name}.bmp".lower()):
            warnings.append(f"texture {texture.name!r} saved as {file_name!r}")
        palette = [tuple(texture.palette[i * 3:i * 3 + 3]) for i in range(256)]
        if len(texture.palette) < 768 or len(texture.pixels) < texture.width * texture.height:
            warnings.append(f"texture {texture.name!r} is truncated in the model")
            palette = [(0, 0, 0)] * 256
            pixels = bytearray(texture.width * texture.height)
        else:
            pixels = bytearray(texture.pixels)
        (out / "maps_8bit" / file_name).write_bytes(write_bmp8(
            Bmp8(texture.width, texture.height, palette, pixels)))  # type: ignore[arg-type]
    family0 = model.skin_families[0] if model.skin_families else list(range(len(model.textures)))

    # -- reference meshes --------------------------------------------------
    sub_raw = [sub.name for part in model.bodyparts for sub in part.models]
    sub_files = iter(_file_names([Path(n.replace("\\", "/")).stem or "studio"
                                  for n in sub_raw]))
    body_lines: list[str] = []
    for part in model.bodyparts:
        entries: list[str] = []
        for sub in part.models:
            file_stem = next(sub_files)
            if not sub.meshes and not sub.vertices:
                entries.append("blank")
                continue
            triangles: list[Triangle] = []
            for mesh in sub.meshes:
                texture_index = family0[mesh.skinref] if mesh.skinref < len(family0) else 0
                texture = model.textures[texture_index] if model.textures else None
                material = tex_names[texture_index] if texture else "missing.bmp"
                width = max(texture.width, 1) if texture else 1
                height = max(texture.height, 1) if texture else 1
                for tri in mesh.triangles:
                    verts = []
                    for tv in tri:
                        bone = sub.vertex_bones[tv.vertex]
                        nbone = sub.normal_bones[tv.normal]
                        pos = worlds[bone].transform_point(Vector3(*sub.vertices[tv.vertex]))
                        nrm = worlds[nbone].rotate_vector(Vector3(*sub.normals[tv.normal]))
                        verts.append(Vertex(bone=bone, position=pos, normal=nrm,
                                            uv=Vector2(tv.s / width, 1.0 - tv.t / height)))
                    # studiomdl reverses SMD winding when it builds strips;
                    # write the reverse of the drawn order (round trip proven)
                    triangles.append(Triangle(material, (verts[1], verts[0], verts[2])))
            smd = Smd(nodes=list(nodes), frames=[bind], triangles=triangles)
            (out / f"{file_stem}.smd").write_text(write_smd_text(smd), encoding="latin-1")
            entries.append(f'studio "{file_stem}"')
        if len(entries) == 1 and entries[0] != "blank":
            body_lines.append(f'$body "{part.name}" "{entries[0][8:-1]}"')
        else:
            body_lines.append(f'$bodygroup "{part.name}"')
            body_lines.append("{")
            body_lines += [f"    {e}" for e in entries]
            body_lines.append("}")

    # -- sequences ---------------------------------------------------------
    seq_files = _file_names([s.label for s in model.sequences])
    seq_lines: list[str] = []
    for index, (seq, file_stem) in enumerate(zip(model.sequences, seq_files, strict=True)):
        paths = []
        for blend in range(max(seq.numblends, 1)):
            try:
                frames = model.sequence_frames(index, blend)
            except Exception as exc:  # noqa: BLE001 - keep decompiling the rest
                warnings.append(f"sequence {seq.label!r}: {exc}")
                frames = [[b.value for b in model.bones]]
            name = file_stem if seq.numblends <= 1 else f"{file_stem}_blend{blend + 1}"
            smd = Smd(nodes=list(nodes),
                      frames=[_frame(t, _smd_pose(model, pose))
                              for t, pose in enumerate(frames)])
            (out / "anims" / f"{name}.smd").write_text(write_smd_text(smd),
                                                       encoding="latin-1")
            paths.append(f'"./anims/{name}"')
        body = [*paths, f"fps {_fmt(seq.fps)}"]
        if seq.flags & STUDIO_LOOPING:
            body.append("loop")
        motion = [name for name, bit in MOTION_FLAGS.items() if seq.motiontype & bit]
        if motion:
            body.append(" ".join(motion))
        if seq.activity:
            if 0 <= seq.activity < len(ACTIVITIES):
                body.append(f"{ACTIVITIES[seq.activity]} {seq.actweight}")
            else:
                warnings.append(f"sequence {seq.label!r}: unknown activity "
                                f"{seq.activity} dropped")
        for k in range(2 if seq.numblends > 1 else 0):
            if seq.blendtype[k]:
                kind = next((n for n, b in MOTION_FLAGS.items() if b == seq.blendtype[k]),
                            None)
                if kind:
                    body.append(f"blend {kind} {_fmt(seq.blendstart[k])} "
                                f"{_fmt(seq.blendend[k])}")
        if seq.entrynode or seq.exitnode:
            if seq.entrynode == seq.exitnode:
                body.append(f"node {seq.entrynode}")
            else:
                word = "rtransition" if seq.nodeflags else "transition"
                body.append(f"{word} {seq.entrynode} {seq.exitnode}")
        for event in seq.events:
            options = f' "{event.options}"' if event.options else ""
            body.append(f"{{ event {event.event} {event.frame}{options} }}")
        seq_lines.append(f"$sequence \"{seq.label}\" {{")
        seq_lines += [f"    {line}" for line in body]
        seq_lines.append("}")

    # -- QC ----------------------------------------------------------------
    lines = [
        "/*",
        f" decompiled by valve-qc-merger from {Path(mdl).name}",
        "*/",
        "",
        f"$modelname {stem}.mdl",
        "$cd .",
        "$cdtexture ./maps_8bit",
        # keep textures at their stored size: without it studiomdl crops each
        # texture to its UV bounds and shifts the texels (512 -> 500x501)
        "$cliptotextures",
    ]
    if any(model.eyeposition):
        lines.append(f"$eyeposition {_vec(model.eyeposition)}")
    if any(model.bbmin) or any(model.bbmax):
        lines.append(f"$bbox {_vec(model.bbmin)} {_vec(model.bbmax)}")
    if any(model.cbmin) or any(model.cbmax):
        lines.append(f"$cbox {_vec(model.cbmin)} {_vec(model.cbmax)}")
    if model.flags:
        lines.append(f"$flags {model.flags}")
    lines.append("")
    for texture, file_name in zip(model.textures, tex_names, strict=True):
        for flag, mode in ((NF_MASKED, "masked"), (NF_ADDITIVE, "additive"),
                           (NF_FULLBRIGHT, "fullbright"), (NF_FLATSHADE, "flatshade")):
            if texture.flags & flag:
                lines.append(f"$texrendermode {file_name} {mode}")
    lines.append("")
    lines += body_lines
    lines.append("")
    if len(model.skin_families) > 1:
        columns = [r for r in range(len(family0))
                   if len({fam[r] for fam in model.skin_families}) > 1]
        if columns:
            lines.append("$texturegroup skinfamilies")
            lines.append("{")
            for fam in model.skin_families:
                row = " ".join(f'"{tex_names[fam[r]]}"' for r in columns)
                lines.append(f"    {{ {row} }}")
            lines.append("}")
            lines.append("")
    for i, att in enumerate(model.attachments):
        lines.append(f'$attachment {i} "{model.bones[att.bone].name}" {_vec(att.org)}')
    for ctrl in model.controllers:
        kind = next((n for n, b in MOTION_FLAGS.items()
                     if b == ctrl.type & ~STUDIO_RLOOP), str(ctrl.type))
        loop = " RLOOP" if ctrl.type & STUDIO_RLOOP else ""
        lines.append(f'$controller {ctrl.index} "{model.bones[ctrl.bone].name}" '
                     f"{kind}{loop} {_fmt(ctrl.start)} {_fmt(ctrl.end)}")
    for box in model.hitboxes:
        lines.append(f'$hbox {box.group} "{model.bones[box.bone].name}" '
                     f"{_vec(box.bbmin)} {_vec(box.bbmax)}")
    lines.append("")
    lines += seq_lines
    qc = out / f"{stem}.qc"
    qc.write_text("\n".join(lines) + "\n", encoding="latin-1")
    return DecompileResult(qc=qc, directory=out, warnings=warnings)


__all__ = ["ACTIVITIES", "DecompileResult", "decompile_mdl"]
