"""merge-players: bodies with donor bones hung elsewhere, or a foreign root."""

from __future__ import annotations

import dataclasses

from valve_qc_merger.merge_players.discovery import load_donor
from valve_qc_merger.merge_players.skeleton import reduce_body
from valve_qc_merger.merge_view.skeleton_ops import fk_worlds, reparent_bone
from valve_qc_merger.models.smd import BonePose, Frame, Node, Triangle
from valve_qc_merger.resources import resource_path
from valve_qc_merger.services.merge_players import MergePlayersOptions


def _donor():  # noqa: ANN202
    from pathlib import Path
    return load_donor(resource_path(MergePlayersOptions(models_dir=Path("."),
                                                        out=Path(".")).base))


def test_body_is_conformed_to_the_donor_skeleton() -> None:
    donor = _donor()
    body = donor.reference.clone()
    names = {n.name for n in body.nodes}
    assert {"Bip01 Head", "Bip01 Spine"} <= names
    world_before = {n.name: fk_worlds(body, body.frames[0])[n.index].translation
                    for n in body.nodes}
    reparent_bone(body, "Bip01 Head", "Bip01 Spine")  # Head off its donor parent
    # an effect bone at the root, carrying a triangle
    index = max(n.index for n in body.nodes) + 1
    body.nodes = [*body.nodes, Node(index, "fx02", -1)]
    body.frames = [Frame(f.time, (*f.poses, BonePose(index, f.poses[0].position,
                                                     f.poses[0].rotation)))
                   for f in body.frames]
    seed = body.triangles[0]
    pinned = tuple(dataclasses.replace(v, bone=index) for v in seed.vertices)
    body.triangles = [*body.triangles, Triangle(seed.material, pinned)]

    collapsed = reduce_body(body, donor)
    assert collapsed == ["fx02"]
    parent = dict(donor.table)
    names_of = {n.index: n.name for n in body.nodes}
    assert all(parent[n.name] == names_of.get(n.parent) for n in body.nodes)
    after = {n.name: fk_worlds(body, body.frames[0])[n.index].translation for n in body.nodes}
    head_a, head_b = after["Bip01 Head"], world_before["Bip01 Head"]
    assert max(abs(head_a.x - head_b.x), abs(head_a.y - head_b.y),
               abs(head_a.z - head_b.z)) < 1e-4  # world pose kept
