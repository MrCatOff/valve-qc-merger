"""Read a GoldSource (Half-Life 1, version 10 or 9) ``.mdl`` into plain dataclasses.

Layouts follow the HLSDK ``studio.h``. Only what a decompile needs is kept;
everything is decoded eagerly except animation frames, which
:meth:`StudioModel.sequence_frames` decodes on demand.

Companion files are read when present next to the model: ``<name>T.mdl``
holds the textures of models compiled with ``$externaltextures``, and
``<name>01.mdl``, ``02``... hold the animations of sequence groups > 0.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

STUDIO_VERSION = 10
# Version 9 (older studiomdl builds; CSO p_/w_ models such as the "infinity"
# pistols) has the version-10 layout byte for byte — every count/offset of
# the header and every struct size line up; its texture names merely lack
# the ".bmp" (the decompiler adds it). Read with the same code; studiomdl
# then compiles the decompile as version 10.
STUDIO_VERSIONS = (9, 10)
HEADER_SIZE = 244

# Motion / controller type bits (studio.h). studiomdl subtracts the linear
# root motion of LX/LY/LZ sequences and stores it in ``linearmovement``.
MOTION_FLAGS = {
    "X": 0x1, "Y": 0x2, "Z": 0x4, "XR": 0x8, "YR": 0x10, "ZR": 0x20,
    "LX": 0x40, "LY": 0x80, "LZ": 0x100, "AX": 0x200, "AY": 0x400, "AZ": 0x800,
    "AXR": 0x1000, "AYR": 0x2000, "AZR": 0x4000,
}
STUDIO_LX, STUDIO_LY, STUDIO_LZ = 0x40, 0x80, 0x100
STUDIO_RLOOP = 0x8000
STUDIO_LOOPING = 0x1

# mstudiotexture_t.flags
NF_FLATSHADE, NF_CHROME, NF_FULLBRIGHT = 0x1, 0x2, 0x4
NF_ADDITIVE, NF_MASKED = 0x20, 0x40


class MdlError(ValueError):
    """Not a readable GoldSource v9/v10 model."""


def _cstr(raw: bytes) -> bytes:
    return raw.split(b"\0", 1)[0]


@dataclass
class Bone:
    name: str
    parent: int
    flags: int
    controller: tuple[int, ...]
    value: tuple[float, ...]  # default pos xyz + rot xyz (radians)
    scale: tuple[float, ...]


@dataclass
class BoneController:
    bone: int
    type: int
    start: float
    end: float
    rest: int
    index: int


@dataclass
class Hitbox:
    bone: int
    group: int
    bbmin: tuple[float, float, float]
    bbmax: tuple[float, float, float]


@dataclass
class Event:
    frame: int
    event: int
    type: int
    options: str


@dataclass
class Sequence:
    label: str
    fps: float
    flags: int
    activity: int
    actweight: int
    events: list[Event]
    numframes: int
    motiontype: int
    motionbone: int
    linearmovement: tuple[float, float, float]
    bbmin: tuple[float, float, float]
    bbmax: tuple[float, float, float]
    numblends: int
    animindex: int
    blendtype: tuple[int, int]
    blendstart: tuple[float, float]
    blendend: tuple[float, float]
    seqgroup: int
    entrynode: int
    exitnode: int
    nodeflags: int


@dataclass
class Attachment:
    name: str
    type: int
    bone: int
    org: tuple[float, float, float]


@dataclass
class Texture:
    name: str  # decoded latin-1 (CSO names are often CP949 bytes)
    raw_name: bytes
    flags: int
    width: int
    height: int
    pixels: bytes  # width * height palette indices, top-down rows
    palette: bytes  # 256 * RGB


@dataclass
class TriVertex:
    vertex: int
    normal: int
    s: int
    t: int


@dataclass
class Mesh:
    skinref: int
    # Triangles as vertex triples in the order studiomdl's GL draw emits them.
    triangles: list[tuple[TriVertex, TriVertex, TriVertex]]


@dataclass
class SubModel:
    name: str
    vertices: list[tuple[float, float, float]]  # bone-local
    vertex_bones: list[int]
    normals: list[tuple[float, float, float]]  # bone-local
    normal_bones: list[int]
    meshes: list[Mesh]


@dataclass
class BodyPart:
    name: str
    base: int
    models: list[SubModel]


@dataclass
class StudioModel:
    path: Path
    name: str
    flags: int
    eyeposition: tuple[float, float, float]
    bbmin: tuple[float, float, float]
    bbmax: tuple[float, float, float]
    cbmin: tuple[float, float, float]
    cbmax: tuple[float, float, float]
    bones: list[Bone]
    controllers: list[BoneController]
    hitboxes: list[Hitbox]
    sequences: list[Sequence]
    seqgroup_names: list[str]
    textures: list[Texture]
    skin_families: list[list[int]]  # [family][skinref] -> texture index
    bodyparts: list[BodyPart]
    attachments: list[Attachment]
    warnings: list[str] = field(default_factory=list)
    _data: bytes = b""
    _groups: dict[int, bytes] = field(default_factory=dict)

    # -- animation ---------------------------------------------------------
    def sequence_frames(
        self, index: int, blend: int = 0,
    ) -> list[list[tuple[float, float, float, float, float, float]]]:
        """Decoded local pose per frame: ``[frame][bone] = (px, py, pz, rx, ry, rz)``.

        Root-bone motion that studiomdl extracted into ``linearmovement``
        (``STUDIO_LX``/``LY``/``LZ``) is added back, as a decompiler must.
        """
        seq = self.sequences[index]
        data = self._data if seq.seqgroup == 0 else self._groups.get(seq.seqgroup)
        if data is None:
            raise MdlError(f"sequence {seq.label!r} lives in sequence group "
                           f"{seq.seqgroup}, whose file is missing")
        frames = max(seq.numframes, 1)
        nbones = len(self.bones)
        base = seq.animindex + blend * nbones * 12
        channels: list[list[list[float]]] = []
        for b, bone in enumerate(self.bones):
            anim = base + b * 12
            offsets = struct.unpack_from("<6H", data, anim)
            per_bone = []
            for j in range(6):
                if offsets[j] == 0:
                    per_bone.append([bone.value[j]] * frames)
                else:
                    raw = _decode_channel(data, anim + offsets[j], frames)
                    per_bone.append([bone.value[j] + v * bone.scale[j] for v in raw])
            channels.append(per_bone)
        out = []
        moves = (seq.motiontype & STUDIO_LX, seq.motiontype & STUDIO_LY,
                 seq.motiontype & STUDIO_LZ)
        for f in range(frames):
            row = []
            for b in range(nbones):
                c = channels[b]
                px, py, pz = c[0][f], c[1][f], c[2][f]
                if self.bones[b].parent == -1 and any(moves) and frames > 1:
                    t = f / (frames - 1)
                    if moves[0]:
                        px += seq.linearmovement[0] * t
                    if moves[1]:
                        py += seq.linearmovement[1] * t
                    if moves[2]:
                        pz += seq.linearmovement[2] * t
                row.append((px, py, pz, c[3][f], c[4][f], c[5][f]))
            out.append(row)
        return out


def _decode_channel(data: bytes, offset: int, frames: int) -> list[int]:
    """Expand one RLE ``mstudioanimvalue_t`` stream to ``frames`` shorts.

    Each run header is (valid, total): ``valid`` explicit values follow,
    and the last of them repeats until ``total`` frames are covered.
    """
    out: list[int] = []
    cursor = offset
    while len(out) < frames:
        valid, total = data[cursor], data[cursor + 1]
        if total == 0:  # malformed stream; hold the last value
            break
        values = struct.unpack_from(f"<{valid}h", data, cursor + 2) if valid else ()
        for k in range(total):
            out.append(values[k] if k < valid else (values[-1] if values else 0))
        cursor += 2 + 2 * valid
    while len(out) < frames:
        out.append(out[-1] if out else 0)
    return out[:frames]


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _vec(data: bytes, offset: int) -> tuple[float, float, float]:
    return struct.unpack_from("<3f", data, offset)


def _read_textures(data: bytes, num: int, index: int) -> list[Texture]:
    textures = []
    for i in range(num):
        off = index + i * 80
        raw = _cstr(data[off:off + 64])
        flags, width, height, pixels_at = struct.unpack_from("<4i", data, off + 64)
        size = width * height
        textures.append(Texture(
            name=raw.decode("latin-1"), raw_name=raw, flags=flags,
            width=width, height=height,
            pixels=data[pixels_at:pixels_at + size],
            palette=data[pixels_at + size:pixels_at + size + 768],
        ))
    return textures


def _read_tricmds(data: bytes, offset: int) -> list[tuple[TriVertex, TriVertex, TriVertex]]:
    """Decode a mesh's triangle commands (strips and fans) into triangles,
    with the winding the HL renderer draws."""
    tris: list[tuple[TriVertex, TriVertex, TriVertex]] = []
    cursor = offset
    while True:
        (count,) = struct.unpack_from("<h", data, cursor)
        cursor += 2
        if count == 0:
            break
        fan = count < 0
        count = abs(count)
        verts = []
        for _ in range(count):
            v, n, s, t = struct.unpack_from("<4h", data, cursor)
            cursor += 8
            verts.append(TriVertex(v, n, s, t))
        for j in range(2, count):
            if fan:
                tris.append((verts[0], verts[j - 1], verts[j]))
            elif j % 2:
                tris.append((verts[j - 1], verts[j - 2], verts[j]))
            else:
                tris.append((verts[j - 2], verts[j - 1], verts[j]))
    return tris


def read_mdl(path: Path) -> StudioModel:
    """Parse ``path`` (and its ``T.mdl`` / ``NN.mdl`` companions)."""
    path = Path(path)
    data = path.read_bytes()
    if len(data) < HEADER_SIZE or data[:4] != b"IDST":
        if data[:4] == b"IDSQ":
            raise MdlError(f"{path.name} is a sequence-group file; open the main model")
        raise MdlError(f"{path.name} is not a GoldSource model (IDST)")
    version = struct.unpack_from("<i", data, 4)[0]
    if version not in STUDIO_VERSIONS:
        raise MdlError(f"{path.name}: studio version {version} (readable: "
                       f"{', '.join(map(str, STUDIO_VERSIONS))})")
    name = _cstr(data[8:72]).decode("latin-1")
    eyeposition = _vec(data, 76)
    bbmin, bbmax = _vec(data, 88), _vec(data, 100)  # header "min/max"
    cbmin, cbmax = _vec(data, 112), _vec(data, 124)  # header "bbmin/bbmax"
    (flags, numbones, boneindex, numcontrollers, controllerindex, numhitboxes,
     hitboxindex, numseq, seqindex, numseqgroups, seqgroupindex, numtextures,
     textureindex, _texturedataindex, numskinref, numskinfamilies, skinindex,
     numbodyparts, bodypartindex, numattachments, attachmentindex) = \
        struct.unpack_from("<21i", data, 136)
    warnings: list[str] = []

    bones = []
    for i in range(numbones):
        off = boneindex + i * 112
        parent, bflags = struct.unpack_from("<2i", data, off + 32)
        bones.append(Bone(
            name=_cstr(data[off:off + 32]).decode("latin-1"),
            parent=parent, flags=bflags,
            controller=struct.unpack_from("<6i", data, off + 40),
            value=struct.unpack_from("<6f", data, off + 64),
            scale=struct.unpack_from("<6f", data, off + 88),
        ))

    controllers = []
    for i in range(numcontrollers):
        bone, ctype, start, end, rest, index = struct.unpack_from(
            "<2i2f2i", data, controllerindex + i * 24)
        controllers.append(BoneController(bone, ctype, start, end, rest, index))

    hitboxes = []
    for i in range(numhitboxes):
        off = hitboxindex + i * 32
        bone, group = struct.unpack_from("<2i", data, off)
        hitboxes.append(Hitbox(bone, group, _vec(data, off + 8), _vec(data, off + 20)))

    seqgroup_names = []
    for i in range(numseqgroups):
        off = seqgroupindex + i * 104
        seqgroup_names.append(_cstr(data[off + 32:off + 96]).decode("latin-1"))

    sequences = []
    for i in range(numseq):
        off = seqindex + i * 176
        label = _cstr(data[off:off + 32]).decode("latin-1")
        fps, sflags, activity, actweight, numevents, eventindex, numframes = \
            struct.unpack_from("<f6i", data, off + 32)
        motiontype, motionbone = struct.unpack_from("<2i", data, off + 68)
        linear = _vec(data, off + 76)
        sbbmin, sbbmax = _vec(data, off + 96), _vec(data, off + 108)
        numblends, animindex = struct.unpack_from("<2i", data, off + 120)
        blendtype = struct.unpack_from("<2i", data, off + 128)
        blendstart = struct.unpack_from("<2f", data, off + 136)
        blendend = struct.unpack_from("<2f", data, off + 144)
        _bparent, seqgroup, entrynode, exitnode, nodeflags = struct.unpack_from(
            "<5i", data, off + 152)
        events = []
        for e in range(numevents):
            eoff = eventindex + e * 76
            frame, event, etype = struct.unpack_from("<3i", data, eoff)
            options = _cstr(data[eoff + 12:eoff + 76]).decode("latin-1")
            events.append(Event(frame, event, etype, options))
        sequences.append(Sequence(
            label, fps, sflags, activity, actweight, events, numframes,
            motiontype, motionbone, linear, sbbmin, sbbmax, numblends, animindex,
            blendtype, blendstart, blendend, seqgroup, entrynode, exitnode,
            nodeflags,
        ))

    # textures: in the model, or in <name>T.mdl ($externaltextures)
    texture_data, tex_num, tex_index = data, numtextures, textureindex
    skin_data, s_ref, s_fam, s_index = data, numskinref, numskinfamilies, skinindex
    if numtextures == 0:
        tpath = path.with_name(path.stem + "T" + path.suffix)
        tpath = tpath if tpath.exists() else path.with_name(path.stem + "t" + path.suffix)
        if tpath.exists():
            texture_data = tpath.read_bytes()
            (tex_num, tex_index, _tdi, s_ref, s_fam, s_index) = struct.unpack_from(
                "<6i", texture_data, 180)
            skin_data = texture_data
        else:
            warnings.append(f"no textures in the model and no {tpath.name}")
    textures = _read_textures(texture_data, tex_num, tex_index)
    skin_families = [
        list(struct.unpack_from(f"<{s_ref}h", skin_data, s_index + f * s_ref * 2))
        for f in range(s_fam)
    ]

    bodyparts = []
    for i in range(numbodyparts):
        off = bodypartindex + i * 76
        bname = _cstr(data[off:off + 64]).decode("latin-1")
        nummodels, base, modelindex = struct.unpack_from("<3i", data, off + 64)
        models = []
        for m in range(nummodels):
            moff = modelindex + m * 112
            mname = _cstr(data[moff:moff + 64]).decode("latin-1")
            (_mtype, _radius, nummesh, meshindex, numverts, vertinfoindex,
             vertindex, numnorms, norminfoindex, normindex) = struct.unpack_from(
                "<if8i", data, moff + 64)
            vertices = [_vec(data, vertindex + k * 12) for k in range(numverts)]
            normals = [_vec(data, normindex + k * 12) for k in range(numnorms)]
            meshes = []
            for k in range(nummesh):
                numtris, triindex, skinref, _nn, _ni = struct.unpack_from(
                    "<5i", data, meshindex + k * 20)
                meshes.append(Mesh(skinref, _read_tricmds(data, triindex)))
            models.append(SubModel(
                name=mname, vertices=vertices,
                vertex_bones=list(data[vertinfoindex:vertinfoindex + numverts]),
                normals=normals,
                normal_bones=list(data[norminfoindex:norminfoindex + numnorms]),
                meshes=meshes,
            ))
        bodyparts.append(BodyPart(bname, base, models))

    attachments = []
    for i in range(numattachments):
        off = attachmentindex + i * 88
        aname = _cstr(data[off:off + 32]).decode("latin-1")
        atype, abone = struct.unpack_from("<2i", data, off + 32)
        attachments.append(Attachment(aname, atype, abone, _vec(data, off + 40)))

    model = StudioModel(
        path=path, name=name, flags=flags, eyeposition=eyeposition,
        bbmin=bbmin, bbmax=bbmax, cbmin=cbmin, cbmax=cbmax, bones=bones,
        controllers=controllers, hitboxes=hitboxes, sequences=sequences,
        seqgroup_names=seqgroup_names, textures=textures,
        skin_families=skin_families, bodyparts=bodyparts,
        attachments=attachments, warnings=warnings, _data=data,
    )
    for group in sorted({s.seqgroup for s in sequences if s.seqgroup > 0}):
        gpath = path.with_name(f"{path.stem}{group:02d}{path.suffix}")
        if gpath.exists():
            gdata = gpath.read_bytes()
            if gdata[:4] != b"IDSQ":
                warnings.append(f"{gpath.name} is not a sequence-group file")
                continue
            model._groups[group] = gdata
        else:
            warnings.append(f"sequence group {group} file {gpath.name} missing")
    return model


__all__ = [
    "Attachment",
    "Bone",
    "BoneController",
    "BodyPart",
    "Event",
    "Hitbox",
    "MdlError",
    "Mesh",
    "Sequence",
    "StudioModel",
    "SubModel",
    "Texture",
    "TriVertex",
    "read_mdl",
]
