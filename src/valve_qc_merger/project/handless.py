"""View models whose hands ARE the model — zombie claws, a gauntlet — or that
have no hands at all (a floating gun). Swapping hands cannot apply to them:
merge-v builds skip their retarget and merge them, as they are, into a part
of their own (``<name>_nohands``, merge-props: no hands bodygroup,
``pev_body`` = the model).

Told from the reference meshes alone (the retarget engine's hand finder,
without loading animations):

- no hand found and nothing named hand / arm (bodygroup, mesh, texture): a
  model without hands;
- under 5 % of the vertices off the hands and the arms holding them: the
  hands are the model (claws);
- ``claw`` in the model's name or its source's path (``zhh/claws/``; a
  model there without a ``v_`` name is imported as a view model too).

Claws with a "weapon" of their own (a tongue, a blade on the arm) look like
any knife here; the Inspector's *Hands are the model* box sets them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

WEAPON_FRACTION = 0.05  # less geometry than this off the hands: claws
_CLAW = re.compile(r"(?i)claw")
_HAND = re.compile(r"(?i)hand|finger|(?:^|[^a-z])(?:fore)?arms?(?:$|[^a-z])")


def is_claw_path(path: Path | str) -> bool:
    """``claw`` in the file's name or one of its folders (``zhh/claws/``)."""
    return bool(_CLAW.search(Path(path).as_posix()))


def hands_are_the_model(folder: Path, source: str | None = None) -> str | None:
    """Why the hands of the decompiled view model in ``folder`` are the model
    (or it has none); None for a weapon held by hands."""
    from valve_qc_merger.handswap import qc as qcmod
    from valve_qc_merger.handswap import smd as smdmod
    from valve_qc_merger.handswap.identify import (
        find_hands,
        merge_skeleton,
        mesh_bone_weights,
    )
    folder = Path(folder)
    if _CLAW.search(folder.name) or (source and _CLAW.search(source)):
        return "claws (named so)"
    qcs = sorted(folder.glob("*.qc"))
    if not qcs:
        return None
    try:
        qc = qcmod.parse(str(qcs[0]))
        refs = {}
        for _group, studio in qc.references:
            base = studio.replace("\\", "/").split("/")[-1]
            path = os.path.join(folder, base + ".smd")
            if not os.path.isfile(path):
                path = os.path.join(folder, base)
            if base not in refs and os.path.isfile(path):
                refs[base] = smdmod.parse(path)
        if not refs:
            return None
        skel = merge_skeleton(refs)
        weights = {name: mesh_bone_weights(s) for name, s in refs.items()}
        labeled = {studio.replace("\\", "/").split("/")[-1]
                   for group, studio in qc.references if re.search("hand", group, re.I)}
        hands = find_hands(skel, refs, weights, labeled, log=lambda *_a: None)
    except Exception:  # noqa: BLE001 - an unreadable model is left to the merge to judge
        return None
    if not hands:
        # a finder miss is no proof: hands without finger bones (v_ak47_beast:
        # shoulder, arm, wrist) are still hands when the model names them
        named = [group for group, _studio in qc.references] + list(refs) + [
            tri.material for smd in refs.values() for tri in smd.triangles[:200]]
        if any(_HAND.search(name) for name in named):
            return None
        return "no hands"
    hand_bones: set[str] = set().union(*(h.core for h in hands))
    for hand in hands:
        bone = skel.parent.get(hand.wrist)
        while bone is not None:  # the arm holding it, up to the root
            hand_bones.add(bone)
            bone = skel.parent.get(bone)
    total = off = 0
    for smd in refs.values():
        names = smd.name_of()
        for tri in smd.triangles:
            for vertex in tri.verts:
                total += 1
                off += names[vertex.dominant_bone()] not in hand_bones
    if total and off / total < WEAPON_FRACTION:
        return "only hands (claws)"
    return None


__all__ = ["WEAPON_FRACTION", "hands_are_the_model", "is_claw_path"]
