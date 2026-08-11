"""CSO hands asset: loads storage/handswap/cso_hands.json.gz (written by
extract_hands.py) and derives the rig structure the retargeter needs.

The asset's bone matrices come from the authored Blender rig, so — unlike
imported SMD bones — their axes ARE meaningful: +Y runs along the bone.
"""
from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass, field

import numpy as np

from .anatomy import hand_frame
from .math3d import inv_rigid


def _project_root() -> str:
    """The valve-qc-merger checkout (dir holding pyproject.toml); cwd for a
    frozen/wheel run."""
    d = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.isfile(os.path.join(d, "pyproject.toml")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.getcwd()
        d = parent


# The CSO hands asset and its per-weapon grip_tuning.json live together under
# storage/handswap/ (project data, not bundled in the package).
DEFAULT_ASSET = os.path.join(_project_root(), "storage", "handswap",
                             "cso_hands.json.gz")

ARM_CHAINS = ("UpperArm", "Arm0", "Arm1")
WRIST = "Hand"
THUMB = "BigFinger"
FINGERS = ("BigFinger", "ForeFinger", "MiddleFinger", "RingFinger",
           "PinkyFinger")


@dataclass
class AssetBone:
    name: str
    parent: str | None
    rest_world: np.ndarray  # 4x4, armature space
    length: float
    rest_local: np.ndarray = None  # filled by loader


@dataclass
class SideRig:
    suffix: str                      # ".R" / ".L"
    wrist: str                       # Hand.R
    arm: list[str]                   # UpperArm, Arm0, Arm1
    chains: list[list[str]]          # 5 finger chains, thumb first
    extras: list[str]                # TweakHand etc. (keep rest local)
    palm_world: np.ndarray = None    # palm frame at rest (4x4)
    palm_local: np.ndarray = None    # Hand_rest^-1 @ palm_world


@dataclass
class HandsAsset:
    bones: dict[str, AssetBone]
    order: list[str]
    triangles: list[dict]            # {mat, corners:[{pos,normal,uv,weights}]}
    sides: dict[str, SideRig] = field(default_factory=dict)  # "right"/"left"

    def bind_inv(self, name: str) -> np.ndarray:
        return inv_rigid(self.bones[name].rest_world)

    def head(self, name: str) -> np.ndarray:
        return self.bones[name].rest_world[:3, 3]

    def tail(self, name: str) -> np.ndarray:
        b = self.bones[name]
        return b.rest_world[:3, 3] + b.rest_world[:3, 1] * b.length


def load(path: str = DEFAULT_ASSET) -> HandsAsset:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        raw = json.load(f)

    bones: dict[str, AssetBone] = {}
    order: list[str] = []
    for b in raw["bones"]:
        bones[b["name"]] = AssetBone(
            name=b["name"], parent=b["parent"],
            rest_world=np.array(b["rest_world"], dtype=float),
            length=float(b["length"]))
        order.append(b["name"])
    for b in bones.values():
        if b.parent:
            b.rest_local = inv_rigid(bones[b.parent].rest_world) @ b.rest_world
        else:
            b.rest_local = b.rest_world.copy()

    asset = HandsAsset(bones=bones, order=order, triangles=raw["triangles"])

    for side, sfx in (("right", ".R"), ("left", ".L")):
        wrist = WRIST + sfx
        if wrist not in bones:
            continue
        chains = []
        for f in FINGERS:
            chain = []
            for i in range(3):
                n = "%s0%d%s" % (f, i, sfx)
                if n in bones:
                    chain.append(n)
            if chain:
                chains.append(chain)
        arm = [a + sfx for a in ARM_CHAINS if a + sfx in bones]
        children_of_wrist = {n for n, b in bones.items() if b.parent == wrist}
        in_chains = {n for ch in chains for n in ch}
        extras = sorted(children_of_wrist - in_chains)

        rig = SideRig(suffix=sfx, wrist=wrist, arm=arm, chains=chains,
                      extras=extras)
        roots = [asset.head(ch[0]) for ch in chains]
        thumb_i = 0  # BigFinger is always first in FINGERS
        anchor = asset.tail(chains[0][-1])
        rig.palm_world, _ = hand_frame(asset.head(wrist), roots,
                                       thumb_i=thumb_i, thumb_anchor=anchor)
        rig.palm_local = inv_rigid(bones[wrist].rest_world) @ rig.palm_world
        asset.sides[side] = rig

    if not asset.sides:
        raise ValueError("asset has no Hand.R/Hand.L bones")
    return asset
