"""Retarget stage-2 scene: a converted model (weapon + hand meshes on one
skeleton) with its animation, posed and probed for hand-into-weapon penetration.

When a foreign weapon is retargeted onto OUR hands, the finger angles transfer
correctly but the WEAPON is not shifted into the right spot, so it clips into the
palm. :class:`CalibScene` skins the model per frame (GoldSource single-bone rigid
skinning, so the posed mesh is engine-exact), lets the caller dial a weapon offset
and per-bone wrist/finger tweaks, and reports the penetration to minimise — the
calibration signal.

Imports numpy (part of the optional ``[calibrate]`` extra) for the vertex arrays
the GUI renders. The penetration probe itself lives in :mod:`.probe` and stays
numpy-free so the bake path can reuse it.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

from valve_qc_merger.calibrate.probe import Vec3, WeaponMesh
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import Smd, Triangle
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.transform import (
    Matrix3,
    Transform,
    axis_angle,
    mat3_multiply,
    mat3_transpose,
)

# One hand's discovered structure: side ("L"/"R"), wrist bone name, and labelled
# finger chains [(label, [bone, ...]), ...].
HandStruct = dict[str, Any]
# Skinned mesh as (points, faces) numpy arrays for pyvista.
MeshArrays = tuple[Any, Any]


def _as_smd(x: Smd | str) -> Smd:
    return parse_smd_file(x) if isinstance(x, str) else x


# --------------------------------------------------------------------------- FK
def _fk(smd: Smd, frame_idx: int, pre: dict[int, Matrix3] | None = None) -> dict[int, Transform]:
    """World transform per bone for one frame (parents resolved first).

    ``pre`` maps a bone index to a parent-local rotation delta pre-multiplied onto
    that bone's local rotation — how a finger tweak bends the bone on top of the
    animation."""
    parent = {n.index: n.parent for n in smd.nodes}
    local = {p.bone: Transform.from_pos_euler(p.position, p.rotation)
             for p in smd.frames[frame_idx].poses}
    if pre:
        for i, d in pre.items():
            if i in local:
                local[i] = Transform(mat3_multiply(d, local[i].rotation), local[i].translation)
    world: dict[int, Transform] = {}
    pend = list(local)
    while pend:
        prog = []
        for i in pend:
            p = parent[i]
            if p == -1:
                world[i] = local[i]
            elif p in world:
                world[i] = world[p].compose(local[i])
            else:
                prog.append(i)
        if len(prog) == len(pend):
            break
        pend = prog
    return world


def _norm(a: Vec3) -> Vec3:
    m = (a[0] * a[0] + a[1] * a[1] + a[2] * a[2]) ** 0.5
    return (a[0] / m, a[1] / m, a[2] / m) if m else a


def _cross3(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _sub3(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _apply3(m: Matrix3, v: Vec3) -> Vec3:
    return (m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
            m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
            m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2])


def discover_hands(
    smd: Smd, bind_world: dict[int, Transform],
) -> tuple[dict[int, tuple[Vec3, Vec3, Vec3]], list[HandStruct]]:
    """Find wrists + finger chains GEOMETRICALLY (correspondence._discover_arms,
    no bone-name dependence — works on our ValveBiped rig AND foreign CSO/Valve
    rigs). Returns (axes_by_index, struct) where axes are the anatomical
    flex/spread/twist unit axes (parent-local) per hand bone, and struct lists
    each hand's wrist + labelled finger chains for the UI."""
    from valve_qc_merger.retarget.correspondence import (
        Rig,
        RigBone,
        _discover_arms,
        _identify_thumb,
        _side_of,
    )
    name = {n.index: n.name for n in smd.nodes}
    parent = {n.index: n.parent for n in smd.nodes}
    idxof = {v: k for k, v in name.items()}
    kids: dict[int, list[int]] = {}
    for i, p in parent.items():
        kids.setdefault(p, []).append(i)

    def pos(i: int) -> Vec3:
        t = bind_world[i].translation
        return (t.x, t.y, t.z)

    bones = []
    for i in name:
        ch = kids.get(i, [])
        head = bind_world[i].translation
        tail = bind_world[ch[0]].translation if ch else Vector3(head.x, head.y, head.z + 1.0)
        bones.append(RigBone(name[i], name[parent[i]] if parent[i] != -1 else None, head, tail))
    rig = Rig(bones)
    try:
        arms = _discover_arms(rig)
    except Exception:  # noqa: BLE001
        return {}, []

    axes: dict[int, tuple[Vec3, Vec3, Vec3]] = {}
    struct: list[HandStruct] = []
    for arm in arms:
        side = _side_of(arm.wrist)
        try:
            thumb = _identify_thumb(rig, arm)
        except Exception:  # noqa: BLE001
            thumb = 0
        wristp = pos(idxof[arm.wrist])
        bases = [pos(idxof[ch[0]]) for ch in arm.fingers]
        nonthumb = [k for k in range(len(bases)) if k != thumb] or list(range(len(bases)))
        n = _norm(_cross3(_sub3(bases[nonthumb[0]], wristp), _sub3(bases[nonthumb[-1]], wristp)))
        chain_names = [arm.wrist] + [b for ch in arm.fingers for b in ch]
        for bn in chain_names:
            i = idxof[bn]
            here = pos(i)
            ch = kids.get(i, [])
            d = _norm(_sub3(pos(ch[0]), here)) if ch else _norm(_sub3(here, pos(parent[i])))
            twist = d
            flex = _norm(_cross3(n, twist))
            spread = _norm(_cross3(twist, flex))
            _ident = ((1, 0, 0), (0, 1, 0), (0, 0, 1))
            pr = bind_world[parent[i]].rotation if parent[i] != -1 else _ident
            prt = mat3_transpose(pr)
            axes[i] = (_apply3(prt, flex), _apply3(prt, spread), _apply3(prt, twist))
        labels = ["index", "middle", "ring", "pinky"]
        fingers = []
        j = 0
        for k, chain in enumerate(arm.fingers):
            if k == thumb:
                fingers.append(("thumb", chain))
            else:
                fingers.append((labels[j] if j < len(labels) else f"finger{j + 2}", chain))
                j += 1
        struct.append(dict(side=side, wrist=arm.wrist, fingers=fingers))
    # label hands left/right; fall back to order when the rig has no L/R names
    if len(struct) == 2 and struct[0]["side"] == struct[1]["side"]:
        for k, h in enumerate(struct):
            h["side"] = "L" if k == 0 else "R"
    return axes, struct


def _triangles_arrays(tris: list[Triangle], world_delta: dict[int, Transform],
                      offset: Vec3 = (0.0, 0.0, 0.0)) -> MeshArrays:
    """Skin a triangle list to (points, faces) numpy arrays for pyvista."""
    pts: list[tuple[float, float, float]] = []
    faces: list[int] = []
    ox, oy, oz = offset
    for t in tris:
        idx = []
        for v in t.vertices:
            d = world_delta[v.bone]
            p = d.transform_point(v.position)
            pts.append((p.x + ox, p.y + oy, p.z + oz))
            idx.append(len(pts) - 1)
        faces.extend((3, idx[0], idx[1], idx[2]))
    return np.asarray(pts, float), np.asarray(faces)


# --------------------------------------------------------------------------- scene
class CalibScene:
    """A converted model (weapon + hand meshes on one skeleton) with an animation.

    ``pose(anim, frame)`` skins the frame once (weapon+hands at zero offset) and
    ``penetration`` reports how deep the hand sits inside the (offset) weapon —
    the calibration signal.
    """

    def __init__(self, weapon_smd: Smd | str, hand_smds: list[Smd | str],
                 anim_smds: dict[str, str]) -> None:
        self.weapon = _as_smd(weapon_smd)  # skeleton + weapon triangles
        self.hands = [_as_smd(h) for h in hand_smds]
        self.anims = {name: parse_smd_file(p) for name, p in anim_smds.items()}
        # mesh bind world (rest) shared by every mesh SMD (same skeleton)
        self._bind = _fk(self.weapon, 0)
        self._cache: dict[tuple[str, int], dict[str, Any]] = {}
        self._name_to_index = {n.name: n.index for n in self.weapon.nodes}
        self._axes, self.hand_struct = discover_hands(self.weapon, self._bind)
        # bone name -> (flex, spread, twist) degrees
        self.tweaks: dict[str, tuple[float, float, float]] = {}

    def set_tweak(self, bone_name: str, flex: float = 0.0, spread: float = 0.0,
                  twist: float = 0.0) -> None:
        """Bend/spread/twist a hand bone by degrees, on top of the animation."""
        self.tweaks[bone_name] = (flex, spread, twist)
        self._cache.clear()

    def _tweak_pre(self) -> dict[int, Matrix3]:
        """Parent-local rotation deltas for the current tweaks, by bone index."""
        pre: dict[int, Matrix3] = {}
        for nm, (f, s, t) in self.tweaks.items():
            i = self._name_to_index.get(nm)
            if i is None or i not in self._axes or (f == 0 and s == 0 and t == 0):
                continue
            af, asp, at = self._axes[i]
            r = axis_angle(Vector3(*af), math.radians(f))
            r = mat3_multiply(axis_angle(Vector3(*asp), math.radians(s)), r)
            r = mat3_multiply(axis_angle(Vector3(*at), math.radians(t)), r)
            pre[i] = r
        return pre

    @classmethod
    def from_combined(cls, mesh_smd: str, anim_smds: dict[str, str],
                      hand_material_hint: str = "hand") -> CalibScene:
        """Load an Approach-B / delta output where hands + weapon share ONE mesh
        SMD (foreign bone names ok). Triangles are split by material: those whose
        texture name contains ``hand_material_hint`` are the hands, the rest the
        weapon. Both keep the full shared skeleton."""
        mesh = parse_smd_file(mesh_smd)
        hint = hand_material_hint.lower()
        hand_tris = [t for t in mesh.triangles if hint in t.material.lower()]
        weap_tris = [t for t in mesh.triangles if hint not in t.material.lower()]
        if not weap_tris or not hand_tris:
            raise ValueError(
                f"material split failed ({len(hand_tris)} hand / {len(weap_tris)} weapon "
                f"tris using hint '{hand_material_hint}'); pass a better hand_material_hint")
        weapon = Smd(nodes=mesh.nodes, frames=mesh.frames, triangles=weap_tris)
        hand = Smd(nodes=mesh.nodes, frames=mesh.frames, triangles=hand_tris)
        return cls(weapon, [hand], anim_smds)

    def anim_names(self) -> list[str]:
        return list(self.anims)

    def frames(self, anim: str) -> int:
        return len(self.anims[anim].frames)

    def _delta(self, anim: str, frame: int) -> dict[int, Transform]:
        world = _fk(self.anims[anim], frame, pre=self._tweak_pre())
        return {i: world[i].compose(self._bind[i].inverse())
                for i in world if i in self._bind}

    def pose(self, anim: str, frame: int) -> dict[str, Any]:
        """Skin the frame ONCE (weapon+hands at zero offset). Cached per (anim,frame).

        The weapon offset is a pure translation applied later at the actor level, so
        it never re-skins: penetration of a hand vertex p against the offset weapon
        equals penetration of (p - offset) against this base (zero-offset) shell.
        """
        key = (anim, frame)
        if key in self._cache:
            return self._cache[key]
        d = self._delta(anim, frame)
        wpts, wfaces = _triangles_arrays(self.weapon.triangles, d)
        hand_meshes = [_triangles_arrays(h.triangles, d) for h in self.hands]
        # unique hand points (dedup) as one numpy array, for the penetration probe
        allpts = np.vstack([hm[0] for hm in hand_meshes]) if hand_meshes else np.zeros((0, 3))
        uniq = np.unique(np.round(allpts, 3), axis=0) if len(allpts) else allpts
        base_weapon = WeaponMesh(self._posed_weapon_tris(d))
        out = dict(weapon=(wpts, wfaces), hands=hand_meshes,
                   hand_points=uniq, base_weapon=base_weapon)
        self._cache[key] = out
        return out

    def auto_seed(self, anim: str, frame: int, offset: list[float], iters: int = 6,
                  damp: float = 0.7, stride: int = 2,
                  min_pen: float = 0.8) -> tuple[list[float], dict[str, float]]:
        """Push the weapon out of GROSS palm intrusion along the push-out normals.

        For a hand vertex sitting ``pen`` deep behind a weapon face with outward
        normal ``n``, shifting the weapon by ``-pen*n`` clears it 1:1 (probe:
        pen' = pen + t·n). Only vertices deeper than ``min_pen`` count — shallow
        overlap is the fingers legitimately gripping the surface, which must stay;
        driving ALL penetration to zero would eject the weapon from the grip. We
        step along the mean push-out of the deep vertices, damped, a few times.
        Returns (new_offset, final_penetration_dict)."""
        s = self.pose(anim, frame)
        bw = s["base_weapon"]
        off = list(offset)
        for _ in range(iters):
            ax = ay = az = 0.0
            cnt = 0
            for p in s["hand_points"][::stride]:
                pp = (p[0] - off[0], p[1] - off[1], p[2] - off[2])
                _d, pn, n = bw.probe(pp, cutoff=6.0)
                if pn > min_pen:
                    ax += pn * n[0]
                    ay += pn * n[1]
                    az += pn * n[2]
                    cnt += 1
            if cnt == 0:
                break
            off[0] -= damp * ax / cnt
            off[1] -= damp * ay / cnt
            off[2] -= damp * az / cnt
        pen = self.penetration(bw, s["hand_points"], (off[0], off[1], off[2]), stride=stride)
        return off, pen

    @staticmethod
    def penetration(base_weapon: WeaponMesh, hand_points: Any, offset: Vec3 = (0.0, 0.0, 0.0),
                    stride: int = 1) -> dict[str, float]:
        """Hand penetration into the weapon translated by ``offset``.

        Probes (hand_point - offset) against the zero-offset shell — no re-skin.
        ``stride`` subsamples the hand points for a fast live estimate (the export
        path uses stride=1 for the exact figure)."""
        ox, oy, oz = offset
        pens: list[float] = []
        for p in hand_points[::stride]:
            pen = base_weapon.probe((p[0] - ox, p[1] - oy, p[2] - oz), cutoff=6.0)[1]
            if pen > 0:
                pens.append(pen)
        pen_max = max(pens) if pens else 0.0
        # scale the summed depth back up when subsampling, so the number is comparable
        pen_sum = float(sum(pens)) * stride
        return dict(pen_max=pen_max, pen_sum=pen_sum, pen_count=len(pens) * stride)

    def _posed_weapon_tris(self, delta: dict[int, Transform]) -> list[Triangle]:
        from valve_qc_merger.models.geometry import Vector2, Vector3
        from valve_qc_merger.models.smd import Vertex
        out = []
        for t in self.weapon.triangles:
            vs = []
            for v in t.vertices:
                p = delta[v.bone].transform_point(v.position)
                vs.append(Vertex(v.bone, Vector3(p.x, p.y, p.z), v.normal, Vector2(0.0, 0.0)))
            out.append(Triangle(t.material, (vs[0], vs[1], vs[2])))
        return out


# --------------------------------------------------------------------------- discovery
def _discover_qc(directory: Path) -> tuple[Smd, Smd, dict[str, str]] | None:
    """Decompiled model (a .qc): split bodygroups into hand vs weapon studios by
    name (any group whose name contains 'hand' is the hands), combine each set
    into one mesh on the shared skeleton, and resolve $sequence anim paths. Both
    hands (rhand+lhand) merge into one hand mesh; multiple weapons (dual elite)
    merge into one weapon mesh. Returns (weapon_smd, hand_smd, anims) or None."""
    from valve_qc_merger.retarget.qc_build import parse_bodygroups, parse_sequences
    qc = next(iter(directory.glob("*.qc")), None)
    if qc is None:
        return None
    text = qc.read_text(errors="replace")

    def resolve(stem: str) -> Path:
        rel = stem.replace("\\", "/")
        return directory / (rel if rel.lower().endswith(".smd") else rel + ".smd")

    hand_paths: list[Path] = []
    weap_paths: list[Path] = []
    for name, studios in parse_bodygroups(text).items():
        target = hand_paths if "hand" in name.lower() else weap_paths
        target.extend(p for s in studios if (p := resolve(s)).exists())
    if not weap_paths:
        return None
    base = parse_smd_file(str(weap_paths[0]))

    def combine(paths: list[Path]) -> Smd:
        tris: list[Triangle] = []
        for p in paths:
            tris.extend(parse_smd_file(str(p)).triangles)
        return Smd(nodes=base.nodes, frames=base.frames, triangles=tris)

    weapon = combine(weap_paths)
    hand = combine(hand_paths) if hand_paths else Smd(nodes=base.nodes, frames=base.frames)
    anims: dict[str, str] = {}
    for seq in parse_sequences(text):
        if seq.smd and (p := resolve(seq.smd)).exists():
            anims[seq.name] = str(p)
    return weapon, hand, anims


def discover(
    directory: Path,
) -> tuple[str, str | Smd, list[str | Smd] | None, dict[str, str]]:
    """Locate the model: retarget output (separate hands_*.smd + anims/), a
    delta/combined output (*_ref.smd + anims/, split by material), or a raw
    decompiled model (a .qc, split by bodygroup). Returns (kind, mesh, hands, anims);
    ``mesh``/``hands`` are file paths except for the qc case, where they are the
    already-combined :class:`Smd` objects. ``hands`` is None only for combined."""
    smds = list(directory.glob("*.smd"))
    anims = {p.stem: str(p) for p in sorted((directory / "anims").glob("*.smd"))}
    hands = sorted(p for p in smds if p.name.startswith("hands_"))
    if hands and anims:
        weapon_smds = [p for p in smds if not p.name.startswith("hands_")]
        return "retarget", str(weapon_smds[0]), [str(h) for h in hands], anims
    ref = [p for p in smds if p.stem.endswith("_ref")]
    if ref and anims:
        return "combined", str(ref[0]), None, anims
    qc = _discover_qc(directory)
    if qc:
        weapon, hand, qanims = qc
        return "qc", weapon, [hand], qanims
    raise ValueError(f"unrecognised model layout in {directory} "
                     "(need a retarget/delta output or a decompiled .qc)")


def scene_from_dir(directory: Path, hand_material_hint: str = "hand") -> CalibScene:
    """Build a :class:`CalibScene` from any recognised output layout in ``directory``."""
    kind, mesh, hands, anims = discover(directory)
    if kind == "combined":
        assert isinstance(mesh, str)  # combined always resolves to a *_ref.smd path
        return CalibScene.from_combined(mesh, anims, hand_material_hint=hand_material_hint)
    assert hands is not None  # only the combined layout has no hands
    return CalibScene(mesh, hands, anims)


__all__ = ["CalibScene", "discover", "scene_from_dir"]
