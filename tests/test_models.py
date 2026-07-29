"""Tests for the model value/data classes."""

from __future__ import annotations

from valve_qc_merger.models import (
    Body,
    BodyGroup,
    Hitbox,
    HitGroup,
    Node,
    Qc,
    Sequence,
    Smd,
    Triangle,
    Vector2,
    Vector3,
    Vertex,
)


def test_vector3_geometry() -> None:
    assert Vector3(3.0, 4.0, 0.0).length() == 5.0
    assert Vector3(0.0, 0.0, 0.0).distance_to(Vector3(0.0, 0.0, 2.0)) == 2.0
    assert Vector3(0.0, 0.0, 0.0).midpoint(Vector3(2.0, 4.0, 6.0)) == Vector3(1.0, 2.0, 3.0)


def test_bounding_box_from_geometry() -> None:
    def vertex(pos: Vector3) -> Vertex:
        return Vertex(bone=0, position=pos, normal=Vector3(0.0, 0.0, 1.0), uv=Vector2(0.0, 0.0))

    smd = Smd(
        triangles=[
            Triangle(
                material="body.bmp",
                vertices=(
                    vertex(Vector3(-1.0, 0.0, 0.0)),
                    vertex(Vector3(1.0, 2.0, 0.0)),
                    vertex(Vector3(0.0, 0.0, 3.0)),
                ),
            )
        ]
    )
    box = smd.bounding_box()
    assert box is not None
    assert box.mins == Vector3(-1.0, 0.0, 0.0)
    assert box.maxs == Vector3(1.0, 2.0, 3.0)
    assert box.size == Vector3(2.0, 2.0, 3.0)
    assert smd.is_reference and not smd.is_animation


def test_smd_node_lookup_and_roots() -> None:
    smd = Smd(nodes=[Node(0, "root", -1), Node(1, "child", 0)])
    assert smd.node_by_name("child") == Node(1, "child", 0)
    assert smd.node_by_index(0) is not None
    assert [n.name for n in smd.root_nodes] == ["root"]
    assert smd.bounding_box() is None


def test_hitbox_head_group() -> None:
    hitbox = Hitbox(group=1, bone="Bip01 Head", mins=Vector3(-4, -4, -4), maxs=Vector3(4, 4, 4))
    assert hitbox.hit_group is HitGroup.HEAD
    assert hitbox.is_head
    assert hitbox.bounds.center == Vector3(0.0, 0.0, 0.0)


def test_qc_lookups_and_sources() -> None:
    qc = Qc(
        modelname="player.mdl",
        bodies=[Body("studio", "reference.smd")],
        body_groups=[BodyGroup("weapon", (Body("knife", "knife.smd"),), has_blank=True)],
        sequences=[Sequence("idle", ("idle.smd",), fps=30.0, loop=True)],
        hitboxes=[Hitbox(1, "Head", Vector3(-4, -4, -4), Vector3(4, 4, 4))],
    )
    assert qc.sequence_names == ["idle"]
    assert qc.find_sequence("idle") is not None
    assert qc.find_sequence("run") is None
    assert qc.sources() == ["reference.smd", "knife.smd", "idle.smd"]
    assert list(qc.hitboxes_by_group()) == [1]
