"""Penetration probe (numpy-free core of retarget stage 2).

The calibration signal: how deep a point sits BEHIND the nearest weapon face,
oriented by the mesh's own vertex normals so open low-poly shells still read
correctly (an enclosure test would miss them).
"""

from __future__ import annotations

from pytest import approx

from valve_qc_merger.calibrate.probe import WeaponMesh
from valve_qc_merger.models.geometry import Vector2, Vector3
from valve_qc_merger.models.smd import Triangle, Vertex


def _tri(a, b, c, normal):
    vs = tuple(Vertex(0, Vector3(*p), Vector3(*normal), Vector2(0.0, 0.0)) for p in (a, b, c))
    return Triangle("weapon_mat", vs)


# One face in the z=0 plane, outward normal +z, covering the (0,0)-(2,0)-(0,2) corner.
_FACE = WeaponMesh([_tri((0, 0, 0), (2, 0, 0), (0, 2, 0), (0, 0, 1))])


def test_point_behind_face_penetrates() -> None:
    dist, pen, n = _FACE.probe((0.5, 0.5, -1.0))
    assert pen == approx(1.0)          # 1u behind the outward face
    assert dist == approx(1.0)         # ...and 1u from the surface
    assert n == approx((0.0, 0.0, 1.0))  # push direction = outward normal


def test_point_in_front_is_outside() -> None:
    _dist, pen, _n = _FACE.probe((0.5, 0.5, 1.0))
    assert pen == approx(-1.0)         # in front of the face => negative penetration


def test_penetration_tracks_a_weapon_shift() -> None:
    # Shifting the weapon by +t along its outward normal clears a behind-point 1:1:
    # probing (p - offset) is how the scene applies the offset without re-skinning.
    p = (0.5, 0.5, -1.0)
    off = (0.0, 0.0, -1.0)  # move the weapon 1u down (away from the point)
    _d, pen, _n = _FACE.probe((p[0] - off[0], p[1] - off[1], p[2] - off[2]))
    assert pen == approx(0.0)          # face now level with the point


def test_vertex_normals_orient_the_face() -> None:
    # Same triangle wound the other way but with +z vertex normals still reads +z
    # outward (the winding-derived normal is flipped to agree with the mesh normals).
    flipped = WeaponMesh([_tri((0, 0, 0), (0, 2, 0), (2, 0, 0), (0, 0, 1))])
    _d, pen, n = flipped.probe((0.5, 0.5, -1.0))
    assert pen == approx(1.0)
    assert n == approx((0.0, 0.0, 1.0))


def test_empty_mesh_returns_cutoff_and_no_normal() -> None:
    empty = WeaponMesh([])
    dist, pen, n = empty.probe((1.0, 2.0, 3.0), cutoff=6.0)
    assert dist == 6.0
    assert pen == 0.0
    assert n == (0.0, 0.0, 0.0)


def test_material_prefix_filters_triangles() -> None:
    tris = [_tri((0, 0, 0), (2, 0, 0), (0, 2, 0), (0, 0, 1))]
    kept = WeaponMesh(tris, material_prefix="weapon")
    dropped = WeaponMesh(tris, material_prefix="hand")
    assert kept.probe((0.5, 0.5, -1.0))[1] == approx(1.0)
    assert dropped.probe((0.5, 0.5, -1.0)) == (1e9, 0.0, (0.0, 0.0, 0.0))
