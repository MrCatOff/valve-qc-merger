"""A hands asset built from any decompiled view model's own hands.

The engine swaps hands for whatever :class:`~.asset.HandsAsset` it is given;
ours comes from the authored Blender rig (``cso_hands.json.gz``). This module
writes the same format from a weapon's hands — e.g. the old small Valve hands
of the classic ``v_deagle`` — so a weapon can be put on *foreign* hands. That
is what the round-trip benchmark needs (:mod:`.bench`): native weapon -> foreign
hands -> back to ours, compared with the native original.

Naming follows the engine's own pairing: the model's hands are planned against
our asset, and each paired chain takes the name of the CSO chain it maps to
(``BigFinger00.R`` …); the wrist becomes ``Hand.<s>`` and up to three arm
ancestors ``Arm1``/``Arm0``/``UpperArm``. Bone frames are rebuilt so +Y runs
along the bone (the asset convention); the mesh keeps its bind positions.
"""

from __future__ import annotations

import gzip
import json
import os
import re

import numpy as np

from . import asset as assetmod
from . import build as buildmod
from .retarget import build_plan

ARM_NAMES = ("Arm1", "Arm0", "UpperArm")  # wrist parent first


def _frame(origin: np.ndarray, y: np.ndarray, hint: np.ndarray) -> np.ndarray:
    """Rigid 4x4 with +Y along ``y`` and X as close to ``hint`` as possible."""
    y = y / (np.linalg.norm(y) or 1.0)
    x = hint - y * float(np.dot(hint, y))
    if np.linalg.norm(x) < 1e-6:
        x = np.cross(y, [0.0, 0.0, 1.0]) if abs(y[2]) < 0.9 else np.cross(y, [1.0, 0.0, 0.0])
    x = x / np.linalg.norm(x)
    z = np.cross(x, y)
    m = np.eye(4)
    m[:3, 0], m[:3, 1], m[:3, 2], m[:3, 3] = x, y, z, origin
    return m


def _ascii_key(name: str) -> str:
    """A file name compared without its non-ASCII characters: decompilers
    write odd bytes (Korean/latin-1) that reach the SMD and the file system
    in different encodings."""
    return "".join(c for c in name.lower() if c.isascii())


def _texture_file(weapon_dir: str, material: str) -> str | None:
    wanted, folded = material.lower(), _ascii_key(material)
    fallback = None
    for root, _dirs, files in os.walk(weapon_dir):
        for name in files:
            if name.lower() == wanted:
                return os.path.join(root, name)
            if fallback is None and _ascii_key(name) == folded:
                fallback = os.path.join(root, name)
    return fallback


def asset_from_weapon(weapon_dir: str, *, prefer: str = "male", keep_materials: bool = False,
                      log=lambda *a: None) -> dict:
    """The raw asset dict (``cso_hands.json.gz`` format) of a weapon's hands.

    ``keep_materials``: the hands keep their own textures (zombie hands carry
    several — skin, claws, effects) instead of the single ``hands.bmp`` slot;
    the asset then lists each material's file and render mode, and the
    engine copies them and writes the ``$texrendermode`` lines."""
    model = buildmod.load_weapon(weapon_dir, None, log=log)
    hand_bones = buildmod.hand_bone_set(model, log=log)
    hand_mats = buildmod.hand_materials(model, hand_bones)
    dropped, _kept = buildmod.classify_meshes(model, hand_bones, hand_mats, log=log)
    if not dropped:
        # hands and weapon in ONE mesh (sting_finger: the syringe): take the
        # mesh mostly on hand bones; the triangle filters below cut the rest
        def share(name: str) -> float:
            weights = model.weights.get(name, {})
            total = sum(weights.values())
            return sum(w for b, w in weights.items() if b in hand_bones) / total if total else 0.0
        dropped = [m for m in model.refs if share(m) > 0.5]
    if not dropped:
        raise ValueError(f"no hand mesh found in {weapon_dir}")
    skel = model.skel
    first = model.qc.sequences[0]["smd"]
    setup = buildmod.anim_world_frames(model.anims[first], skel)[0]
    plan = build_plan(assetmod.load(), skel, model.hands, setup, log=log)

    rename: dict[str, str] = {}
    arm_of: dict[str, list[str]] = {}
    for sp in plan.sides:
        sfx = sp.rig.suffix
        rename[sp.hand.wrist] = "Hand" + sfx
        arm, parent = [], skel.parent.get(sp.hand.wrist)
        while parent is not None and parent in hand_bones and len(arm) < len(ARM_NAMES):
            arm.append(parent)
            parent = skel.parent.get(parent)
        for old, new in zip(arm, ARM_NAMES, strict=False):
            rename[old] = new + sfx
        arm_of[sp.side] = arm
        for old_chain, cso_chain in sp.pairs:
            for old, new in zip(old_chain, cso_chain, strict=False):
                rename[old] = new

    def head(name: str) -> np.ndarray:
        return skel.bind_world[name][:3, 3]

    bones = []
    children = {old: [c for c in skel.children.get(old, []) if c in rename] for old in rename}
    for old, new in rename.items():
        kids = children[old]
        if new.startswith("Hand"):
            sp = next(s for s in plan.sides if s.hand.wrist == old)
            target = np.mean([head(ch[0]) for ch, _c in sp.pairs], axis=0)
        elif kids:
            target = head(kids[0])
        else:
            side_hand = next(s.hand for s in plan.sides
                             if any(old in ch for ch in s.hand.chains))
            direction = side_hand.tip_dirs.get(old)
            parent_old = skel.parent.get(old)
            span = np.linalg.norm(head(old) - head(parent_old)) if parent_old else 1.0
            target = head(old) + (direction if direction is not None
                                  else head(old) - head(parent_old)) * span * 0.8
        y = target - head(old)
        length = float(np.linalg.norm(y)) or 0.5
        parent = skel.parent.get(old)
        while parent is not None and parent not in rename:
            parent = skel.parent.get(parent)
        bones.append({
            "name": new, "parent": rename.get(parent) if parent else None,
            "rest_world": _frame(head(old), y, skel.bind_world[old][:3, 0]).tolist(),
            "length": length,
        })
    order = {b: i for i, b in enumerate(skel.names)}
    bones.sort(key=lambda b: order[next(o for o, n in rename.items() if n == b["name"])])
    bones = _complete_arms(bones, plan)

    meshes = sorted(dropped)
    wanted = [m for m in meshes if prefer in m.lower() and "fe" + prefer not in m.lower()]
    mesh = (wanted or meshes)[0]
    smd = model.refs[mesh]
    name_of = smd.name_of()

    owners: dict[str, str] = {}

    def owner(bone_id: int) -> str:
        """The hand-rig bone a vertex's bone rides: its nearest mapped
        ancestor, else (forearm twist bones hanging off the upper arm, as in
        3ds Max Biped rigs) the mapped bone nearest to it."""
        start = name_of[bone_id]
        if start in owners:
            return owners[start]
        name = start
        while name is not None and name not in rename:
            name = skel.parent.get(name)
        if name is None:
            here = head(start)
            name = min(rename, key=lambda b: float(np.linalg.norm(head(b) - here)))
        owners[start] = rename[name]
        return owners[start]

    textures: dict[str, str] = {}
    render_modes: dict[str, str] = {}
    if keep_materials:
        qc_text = open(model.qc.path, encoding="latin-1").read()
        modes = {m.group(1).lower(): m.group(2) for m in re.finditer(
            r'\$texrendermode\s+"?([^"\n]+?\.bmp)"?\s+(\w+)', qc_text, re.IGNORECASE)}

    def material(name: str) -> str:
        if not keep_materials:
            return "hands.bmp"
        clean = re.sub(r"[^A-Za-z0-9_.-]", "_", name).lstrip("_") or "hand.bmp"
        if clean not in textures:
            found = _texture_file(weapon_dir, name)
            if found:
                textures[clean] = found
            if name.lower() in modes:
                render_modes[clean] = modes[name.lower()]
        return clean

    # props hanging off the hand (voodoo's doll on the wrist): a separate
    # piece of mesh none of whose vertices sits on the hand's own bones
    # (wrist, fingers, arm) is not hand, whatever bone carries it
    anatomical = set(rename)
    piece = list(range(len(smd.triangles)))

    def root(i: int) -> int:
        while piece[i] != i:
            piece[i] = piece[piece[i]]
            i = piece[i]
        return i
    seen: dict[tuple, int] = {}
    for i, tri in enumerate(smd.triangles):
        for v in tri.verts:
            key = tuple(np.round(np.asarray(v.pos, dtype=float), 4))
            if key in seen:
                piece[root(i)] = root(seen[key])
            else:
                seen[key] = i
    hand_pieces = {root(i) for i, tri in enumerate(smd.triangles)
                   if any(name_of[v.dominant_bone()] in anatomical for v in tri.verts)}

    triangles = []
    skipped = 0
    for index, tri in enumerate(smd.triangles):
        if root(index) not in hand_pieces:
            skipped += 1
            continue
        # only the HANDS: a weapon modelled into the hand mesh (v_heavy_knife:
        # the blade rides 'Bone_Knife' with its own texture) must stay out
        if hand_mats and tri.material not in hand_mats \
                or any(name_of[v.dominant_bone()] not in hand_bones for v in tri.verts):
            skipped += 1
            continue
        # a triangle bridging the two arms (voodoo's sleeves are stitched
        # together) tears as soon as the hands move apart: leave it out
        if len({owner(v.dominant_bone())[-2:] for v in tri.verts}) > 1:
            skipped += 1
            continue
        corners = []
        for v in tri.verts:
            weights: dict[str, float] = {}
            for b, w in v.weights():
                weights[owner(b)] = weights.get(owner(b), 0.0) + float(w)
            corners.append({"pos": [float(c) for c in v.pos],
                            "normal": [float(c) for c in v.normal],
                            "uv": [float(c) for c in v.uv],
                            "weights": [[k, w] for k, w in weights.items()]})
        triangles.append({"mat": material(tri.material), "corners": corners})
    log("foreign hands from %s: mesh %r, %d bones, %d triangles (%d weapon/prop triangles left out)"
        % (os.path.basename(weapon_dir), mesh, len(bones), len(triangles), skipped))
    raw = {"source_blend": f"weapon:{os.path.basename(os.path.abspath(weapon_dir))}/{mesh}",
           "bones": bones, "triangles": triangles}
    if keep_materials:
        raw["textures"], raw["render_modes"] = textures, render_modes
    return raw


def _complete_arms(bones: list[dict], plan) -> list[dict]:  # noqa: ANN001
    """The solver drives a three-bone arm (UpperArm -> Arm0 -> Arm1 -> Hand).
    Rigs with a shorter arm (Valve hands: a forearm only) get the missing
    bones synthesised on the line of the forearm, no mesh on them: Arm0 at
    the forearm's head (the elbow), UpperArm one forearm length further."""
    by_name = {b["name"]: b for b in bones}
    added: list[dict] = []
    for sp in plan.sides:
        sfx = sp.rig.suffix
        wrist = by_name["Hand" + sfx]
        present = [n + sfx for n in reversed(ARM_NAMES) if n + sfx in by_name]
        if len(present) == len(ARM_NAMES):
            continue
        top = by_name[present[0]] if present else wrist  # the highest arm bone
        top_head = np.array(top["rest_world"])[:3, 3]
        wrist_head = np.array(wrist["rest_world"])[:3, 3]
        along = top_head - wrist_head  # wrist -> elbow
        if np.linalg.norm(along) < 1e-6:
            along = -np.array(wrist["rest_world"])[:3, 1]
        span = float(np.linalg.norm(along)) or 6.0
        along = along / (np.linalg.norm(along) or 1.0)
        missing = [n + sfx for n in reversed(ARM_NAMES) if n + sfx not in by_name]
        parent = None
        heads = {}
        for i, name in enumerate(missing):  # from the shoulder down
            heads[name] = top_head + along * span * (len(missing) - i - 1) * 1.0
            if i == len(missing) - 1 and present:
                heads[name] = top_head + along * 1e-3  # just above the real top
            y = (top_head if i == len(missing) - 1 else
                 top_head + along * span * (len(missing) - i - 2)) - heads[name]
            if np.linalg.norm(y) < 1e-6:
                y = -along
            bone = {"name": name, "parent": parent,
                    "rest_world": _frame(heads[name], y,
                                         np.array(top["rest_world"])[:3, 0]).tolist(),
                    "length": float(np.linalg.norm(y)) or 1e-3}
            added.append(bone)
            parent = name
        top["parent"] = parent
    return added + bones


def write_asset(raw: dict, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(raw, f)
    return path


__all__ = ["asset_from_weapon", "write_asset"]
