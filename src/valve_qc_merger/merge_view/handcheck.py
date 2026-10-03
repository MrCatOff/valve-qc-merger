"""Do these models wear the same hands? (``--shared-hands`` guard)

The shared-hands merge emits ONE hands bodygroup — the first model's hand
meshes — for every weapon, so every model must wear the same hands. The bind
pose differs per weapon (the retarget seats the hands at each weapon's grip),
so meshes are compared in BONE-LOCAL space: each vertex expressed in its
bone's bind frame. Retargeted weapons agree to SMD precision (~1e-5 u); a
model still wearing its own hands differs by units, even after its bones
were renamed to the canonical names.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from valve_qc_merger.merge_view.skeleton_ops import fk_worlds
from valve_qc_merger.models.smd import Smd

TOLERANCE = 0.05  # units; SMD files carry 6 decimals, retargets agree to ~1e-5


@dataclass(frozen=True)
class HandShape:
    """One hand mesh: bone name and bone-local position per vertex, in
    triangle order."""

    bones: tuple[str, ...]
    local: np.ndarray  # (n, 3)


def hand_shape(smd: Smd) -> HandShape:
    worlds = fk_worlds(smd, smd.frames[0])
    inverse = {index: xf.inverse() for index, xf in worlds.items()}
    name_of = {n.index: n.name for n in smd.nodes}
    bones: list[str] = []
    local: list[tuple[float, float, float]] = []
    for tri in smd.triangles:
        for v in tri.vertices:
            p = inverse[v.bone].transform_point(v.position)
            bones.append(name_of[v.bone])
            local.append((p.x, p.y, p.z))
    return HandShape(tuple(bones), np.asarray(local, dtype=float).reshape(-1, 3))


def _per_bone_sorted(shape: HandShape) -> dict[str, np.ndarray]:
    out: dict[str, list[tuple[float, float, float]]] = {}
    for bone, point in zip(shape.bones, shape.local, strict=True):
        out.setdefault(bone, []).append(tuple(point))
    return {bone: np.asarray(sorted(points)) for bone, points in out.items()}


def shape_distance(a: HandShape, b: HandShape) -> float:
    """Largest vertex mismatch (inf when the meshes are not comparable)."""
    if len(a.bones) != len(b.bones):
        return float("inf")
    if not len(a.bones):
        return 0.0
    # same triangle order: compare vertex by vertex, whatever the bones are
    # called (names are only a rig convention)
    direct = float(np.abs(a.local - b.local).max())
    if a.bones == b.bones or direct <= TOLERANCE:
        return direct
    sa, sb = _per_bone_sorted(a), _per_bone_sorted(b)
    if sa.keys() != sb.keys() or any(len(sa[k]) != len(sb[k]) for k in sa):
        return direct
    return min(direct, max((float(np.abs(sa[k] - sb[k]).max()) for k in sa), default=0.0))


def same_hands(a: list[HandShape], b: list[HandShape], tol: float = TOLERANCE) -> bool:
    """Every hand variant matches, in order."""
    return len(a) == len(b) and all(shape_distance(x, y) <= tol for x, y in zip(a, b,
                                                                                 strict=True))


def group_by_hands(entries: list[tuple[str, list[HandShape]]],
                   tol: float = TOLERANCE) -> list[list[str]]:
    """Cluster model names by identical hands, the largest cluster first
    (ties: the one holding the earliest model)."""
    clusters: list[tuple[list[HandShape], list[str]]] = []
    for name, shapes in entries:
        for key, members in clusters:
            if same_hands(key, shapes, tol):
                members.append(name)
                break
        else:
            clusters.append((shapes, [name]))
    order = sorted(range(len(clusters)), key=lambda i: (-len(clusters[i][1]), i))
    return [clusters[i][1] for i in order]


def local_distance(a: HandShape, b: HandShape) -> float:
    """Like :func:`shape_distance` but blind to bone NAMES: the meshes match
    when every vertex sits at the same place in its bone's frame, whatever
    the bones are called (rigs rename freely; ``Hand.L`` is just Blender's
    convention)."""
    if a.local.shape != b.local.shape:
        return float("inf")
    return float(np.abs(a.local - b.local).max()) if len(a.local) else 0.0


def triangle_count(path: Path) -> int:
    """Triangles of an SMD, counted from its lines (no parsing)."""
    count = 0
    inside = False
    try:
        with open(path, encoding="latin-1") as handle:
            for line in handle:
                text = line.strip()
                if not inside:
                    inside = text == "triangles"
                elif text == "end":
                    break
                elif text:
                    count += 1
    except OSError:
        return 0
    return count // 4


_REFERENCES: dict[tuple[str, int], HandShape] = {}
_VERDICTS: dict[tuple[str, int, int, str], bool] = {}


def _reference_shape(reference: Path) -> HandShape:
    from valve_qc_merger.parsers.smd import parse_smd_file
    key = (str(reference), reference.stat().st_mtime_ns)
    if key not in _REFERENCES:
        _REFERENCES[key] = hand_shape(parse_smd_file(reference))
    return _REFERENCES[key]


def smd_is_reference(path: Path, reference: Path, tol: float = TOLERANCE) -> bool:
    """Is the SMD at ``path`` the ``reference`` hand mesh (bone-local, names
    ignored)? Cached by file time and size; files with another triangle
    count are rejected without being parsed."""
    from valve_qc_merger.parsers.smd import parse_smd_file
    try:
        stat = path.stat()
    except OSError:
        return False
    key = (str(path), stat.st_mtime_ns, stat.st_size, str(reference))
    if key not in _VERDICTS:
        want = _reference_shape(reference)
        verdict = False
        if triangle_count(path) * 3 == len(want.local):
            try:
                verdict = local_distance(hand_shape(parse_smd_file(path)), want) <= tol
            except Exception:  # noqa: BLE001 - an unreadable mesh is not our hands
                verdict = False
        _VERDICTS[key] = verdict
    return _VERDICTS[key]


def dir_wears_hands(model_dir: Path, reference: Path, tol: float = TOLERANCE) -> bool:
    """One of the model's reference SMDs (next to its QC) is ``reference``."""
    return any(smd_is_reference(p, reference, tol) for p in sorted(model_dir.glob("*.smd")))


__all__ = ["HandShape", "TOLERANCE", "dir_wears_hands", "group_by_hands", "hand_shape",
           "local_distance", "same_hands", "shape_distance", "smd_is_reference",
           "triangle_count"]
