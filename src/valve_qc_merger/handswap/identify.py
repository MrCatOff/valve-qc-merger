"""Identify the ORIGINAL hand bones of a decompiled viewmodel.

Bone names in decompiled models are unreliable ('BoneXX'), so hands are
found from data: skin weights + skeleton topology. A palm is the bone
where the hierarchy fans out into >=4 finger-like chains; the wrist is the
nearest weighted bone at or above the fan. Left/right comes from the
reference mesh names when possible, otherwise from palm chirality vs the
weapon geometry direction.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from .anatomy import hand_frame
from .math3d import local_matrix
from .smd import Smd


@dataclass
class OrigSkeleton:
    names: list[str]
    parent: dict[str, str | None]
    children: dict[str, list[str]]
    bind_local: dict[str, tuple[np.ndarray, np.ndarray]]
    bind_world: dict[str, np.ndarray]

    def ancestors(self, name: str):
        p = self.parent.get(name)
        while p is not None:
            yield p
            p = self.parent.get(p)

    def subtree(self, name: str) -> set[str]:
        out, stack = set(), [name]
        while stack:
            b = stack.pop()
            out.add(b)
            stack.extend(self.children.get(b, ()))
        return out

    def world_at(self, frame_locals: dict[str, tuple]) -> dict[str, np.ndarray]:
        """FK by name; bones missing from frame_locals hold their bind."""
        out: dict[str, np.ndarray] = {}

        def build(n: str) -> np.ndarray:
            if n in out:
                return out[n]
            pos, rot = frame_locals.get(n, self.bind_local[n])
            m = local_matrix(pos, rot)
            p = self.parent[n]
            if p is not None:
                m = build(p) @ m
            out[n] = m
            return m

        for n in self.names:
            build(n)
        return out


@dataclass
class HandInfo:
    side: str                       # "right" / "left" / "?"
    wrist: str
    fan: str
    chains: list[list[str]]
    tip_dirs: dict[str, np.ndarray]
    core: set[str]                  # bones that ARE the hand (to delete)
    source_meshes: set[str] = field(default_factory=set)


def merge_skeleton(refs: dict[str, Smd]) -> OrigSkeleton:
    """Union of all reference SMD skeletons, matched by name (like
    studiomdl). First file to define a bone wins its parent + bind key."""
    parent: dict[str, str | None] = {}
    bind_local: dict[str, tuple] = {}
    names: list[str] = []
    for smd in refs.values():
        n_of = smd.name_of()
        for node in smd.nodes:
            if node.name in parent:
                continue
            parent[node.name] = n_of[node.parent] if node.parent >= 0 else None
            pos, rot = smd.frames[0][node.id]
            bind_local[node.name] = (pos.copy(), rot.copy())
            names.append(node.name)
    children: dict[str, list[str]] = {n: [] for n in names}
    for n in names:
        p = parent[n]
        if p is not None:
            children[p].append(n)
    skel = OrigSkeleton(names=names, parent=parent, children=children,
                        bind_local=bind_local, bind_world={})
    skel.bind_world = skel.world_at({})
    return skel


def mesh_bone_weights(smd: Smd) -> dict[str, float]:
    n_of = smd.name_of()
    totals: dict[str, float] = {}
    for tri in smd.triangles:
        for v in tri.verts:
            for b, w in v.weights():
                if w > 1e-4:
                    totals[n_of[b]] = totals.get(n_of[b], 0.0) + w
    return totals


def _finger_chains_of(skel: OrigSkeleton, bone: str, weighted: set[str],
                      total_weight: dict[str, float] | None = None
                      ) -> list[list[str]]:
    """Chains of length>=2 hanging off `bone` that look like FINGERS.

    Weapon subtrees are often parented to the wrist too (famas: STOCK
    under 'Bip01 L Hand') and would register as a sixth 'finger', get
    counted into the hand and delete the whole weapon — so chains that
    are spatial or weight outliers vs. their siblings are rejected."""
    chains = []
    for c in skel.children.get(bone, ()):
        sub = skel.subtree(c)
        if not (sub & weighted):
            continue
        chain, cur = [c], c
        while skel.children.get(cur):
            kids = [k for k in skel.children[cur]
                    if skel.subtree(k) & weighted] or skel.children[cur]
            cur = kids[0]
            chain.append(cur)
        if len(chain) >= 2:
            chains.append(chain)
    if len(chains) < 3 or total_weight is None:
        return chains

    origin = skel.bind_world[bone][:3, 3]

    def extent(ch):
        sub = skel.subtree(ch[0])
        return max(np.linalg.norm(skel.bind_world[b][:3, 3] - origin)
                   for b in sub)

    def weight(ch):
        return sum(total_weight.get(b, 0.0) for b in skel.subtree(ch[0]))

    exts = sorted(extent(c) for c in chains)
    wts = sorted(weight(c) for c in chains)
    med_e = exts[len(exts) // 2]
    med_w = max(wts[len(wts) // 2], 1e-6)
    kept = [c for c in chains
            if extent(c) <= 2.5 * med_e and weight(c) <= 8 * med_w]
    if len(kept) > 5:  # still too many: the 5 most compact win
        kept.sort(key=extent)
        kept = kept[:5]
    return kept


def find_hands(skel: OrigSkeleton, refs: dict[str, Smd],
               weights_by_mesh: dict[str, dict[str, float]],
               hand_labeled: set[str] | None = None,
               log=print) -> list[HandInfo]:
    weighted = {b for w in weights_by_mesh.values() for b in w}
    total_weight: dict[str, float] = {}
    for w in weights_by_mesh.values():
        for b, x in w.items():
            total_weight[b] = total_weight.get(b, 0.0) + x

    # --- palm fans: >=4 finger chains (fall back to 3) -------------------
    fans = []
    for min_chains in (4, 3):
        for b in skel.names:
            chains = _finger_chains_of(skel, b, weighted, total_weight)
            if len(chains) >= min_chains:
                fans.append((b, chains))
        if fans:
            break
    # deepest-only: drop a fan that is an ancestor of another fan
    fan_names = {f for f, _ in fans}
    fans = [(f, ch) for f, ch in fans
            if not any(a in fan_names for a in _descendant_fans(skel, f,
                                                                fan_names))]
    if len(fans) > 2:
        # keep the two with the most chains / deepest
        fans.sort(key=lambda fc: -len(fc[1]))
        fans = fans[:2]

    hands = []
    for fan, chains in fans:
        wrist = fan
        while wrist is not None and wrist not in weighted:
            wrist = skel.parent[wrist]
        if wrist is None:
            wrist = fan
        # the hand CORE is fan + finger chains + path to the wrist — NOT
        # the whole fan subtree: weapon bones may hang off the wrist too
        core = {fan, wrist}
        for ch in chains:
            core |= skel.subtree(ch[0])
        for a in skel.ancestors(fan):
            core.add(a)
            if a == wrist:
                break
        tip_dirs = _tip_directions(skel, refs, chains)
        srcs = {m for m, w in weights_by_mesh.items()
                if sum(w.get(b, 0.0) for b in core) > 0.5}
        hands.append(HandInfo(side="?", wrist=wrist, fan=fan, chains=chains,
                              tip_dirs=tip_dirs, core=core,
                              source_meshes=srcs))
        log("  hand fan %r: wrist %r, %d fingers, meshes %s"
            % (fan, wrist, len(chains), sorted(srcs)))

    # Reject false hands: a weapon whose own bone tree fans out like fingers
    # (a "connector"/effect rig) registers as a hand skinned only by the
    # WEAPON mesh. When the QC labels the real hand meshes, drop any fan not
    # backed by one of them — as long as a real (labelled) hand survives.
    if hand_labeled:
        real = [h for h in hands if h.source_meshes & hand_labeled]
        if real and len(real) < len(hands):
            for h in hands:
                if h not in real:
                    log("  dropping false hand fan %r (weapon-mesh only)"
                        % h.fan)
            hands = real

    _resolve_sides(skel, hands, weights_by_mesh, log)
    return hands


def _descendant_fans(skel, fan, fan_names):
    return (skel.subtree(fan) - {fan}) & fan_names


def _tip_directions(skel, refs, chains):
    """Leaf phalanx direction = skinned-vertex centroid - bone head (the
    only reliable direction for a chain's last bone: SMD tails are stubs
    and axes are arbitrary)."""
    tips = {}
    leaves = {ch[-1] for ch in chains}
    acc = {b: np.zeros(3) for b in leaves}
    tot = {b: 0.0 for b in leaves}
    for smd in refs.values():
        n_of = smd.name_of()
        for tri in smd.triangles:
            for v in tri.verts:
                for b, w in v.weights():
                    name = n_of[b]
                    if name in leaves and w > 1e-4:
                        acc[name] += v.pos * w
                        tot[name] += w
    for b in leaves:
        if tot[b] > 0:
            d = acc[b] / tot[b] - skel.bind_world[b][:3, 3]
            n = np.linalg.norm(d)
            if n > 1e-4:
                tips[b] = d / n
    return tips


_RIGHT = re.compile(r'(?:^|[^a-z])(?:r|right|rhand|righthand)(?:[^a-z]|$)')
_LEFT = re.compile(r'(?:^|[^a-z])(?:l|left|lhand|lefthand)(?:[^a-z]|$)')


def _name_side(text: str) -> str | None:
    t = text.lower().replace("_", " ")
    r = bool(_RIGHT.search(t)) or "rhand" in t or "righthand" in t
    l = bool(_LEFT.search(t)) or "lhand" in t or "lefthand" in t
    if r and not l:
        return "right"
    if l and not r:
        return "left"
    return None


def _mesh_side(h) -> str | None:
    for src in sorted(h.source_meshes):
        s = _name_side(src)
        if s:
            return s
    return None


def _bone_side(h) -> str | None:
    return _name_side(h.wrist) or _name_side(h.fan)


def _chirality_scores(skel, hands) -> list[float]:
    """Per-hand palm-normal · direction-to-weapon. Calibrated on stock
    models: the RIGHT hand's palm normal points TOWARD the weapon centroid,
    so a higher score = more right-handed."""
    hand_bones = set()
    for h in hands:
        hand_bones |= h.core
    weapon_heads = [skel.bind_world[b][:3, 3]
                    for b in set(skel.names) - hand_bones]
    wc = np.mean(weapon_heads, axis=0) if weapon_heads else np.zeros(3)
    scores = []
    for h in hands:
        roots = [skel.bind_world[ch[0]][:3, 3] for ch in h.chains]
        frame, _ = hand_frame(skel.bind_world[h.wrist][:3, 3], roots)
        scores.append(float(np.dot(frame[:3, 2], wc - frame[:3, 3])))
    return scores


def _resolve_sides(skel, hands, weights_by_mesh, log):
    if len(hands) == 2:
        # Two hands MUST end up on opposite sides. Prefer the first signal
        # that already separates them: the wrist BONE name
        # (Bone_Lefthand/Bone_Righthand) distinguishes CSO models whose
        # male/female hand MESHES share one L/R name; the mesh name
        # distinguishes stock models (rhand/lhand). Palm chirality is the
        # geometric fallback when neither name tells them apart.
        for hint, why in ((_bone_side, "wrist name"),
                          (_mesh_side, "mesh name")):
            s0, s1 = hint(hands[0]), hint(hands[1])
            if s0 and s1 and s0 != s1:
                hands[0].side, hands[1].side = s0, s1
                log("  sides from %s: %s / %s" % (why, s0, s1))
                break
        else:
            scores = _chirality_scores(skel, hands)
            right = 0 if scores[0] >= scores[1] else 1
            hands[right].side = "right"
            hands[1 - right].side = "left"
            log("  sides from chirality: scores %s" % [round(x, 2) for x in scores])
    else:
        for h in hands:
            h.side = _mesh_side(h) or _bone_side(h) or "?"
        unresolved = [h for h in hands if h.side == "?"]
        if unresolved:
            scores = _chirality_scores(skel, unresolved)
            for h, sc in zip(unresolved, scores):
                h.side = "right" if sc > 0 else "left"
                log("  chirality: fan %r -> %s hand" % (h.fan, h.side))

    for h in hands:
        log("  %s hand: wrist %r fan %r" % (h.side, h.wrist, h.fan))
