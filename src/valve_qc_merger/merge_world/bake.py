"""Rendered-pose baking for merge-world.

A w_ model draws at ``anim_world . bind_world⁻¹ . vertex`` (per bone). The
corpus idles are static poses (often padded to 101 identical frames) that do
NOT always equal the bind pose — the infinity series sits 9+ units from its
bind. Baking that per-bone rigid transform into the vertices makes every
model's mesh literally live in its on-screen space, after which all vertices
can share one identity bone with zero loss.

Models whose idle equals their bind (the majority) get an identity transform,
so their vertices stay bit-identical.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from valve_qc_merger.merge_view.discovery import ModelInput
from valve_qc_merger.merge_view.skeleton_ops import fk_worlds
from valve_qc_merger.models.smd import Vertex
from valve_qc_merger.transform import Transform


@dataclass
class WorldPlan:
    """Result of baking one model: diagnostics only (the mesh is mutated)."""

    model: str
    max_bake_delta: float = 0.0  # how far the idle pose moved the mesh
    warnings: list[str] = field(default_factory=list)


def bake_rendered_pose(model: ModelInput) -> WorldPlan:
    """Bake ``idle . bind⁻¹`` into every mesh vertex, in place.

    Uses frame 0 of the model's idle animation (every corpus idle is static;
    a moving idle would be a modelling error for a dropped weapon). Models
    without any animation render at their bind pose — identity transform.
    """
    plan = WorldPlan(model=model.name)
    anim = next(iter(model.anims.values()), None)
    anim_worlds: dict[str, Transform] = {}
    if anim is not None and anim.frames:
        worlds = fk_worlds(anim, anim.frames[0])
        anim_worlds = {n.name: worlds[n.index] for n in anim.nodes}
        if len(anim.frames) > 1:
            first = anim.frames[0].poses
            moving = any(
                frame.poses != first for frame in anim.frames[1:]
            )
            if moving:
                plan.warnings.append(
                    "idle animation is not a static pose; baking frame 0"
                )

    for mesh in model.meshes.values():
        if not mesh.frames or not mesh.triangles:
            continue
        bind_worlds = fk_worlds(mesh, mesh.frames[0])
        transforms: dict[int, Transform] = {}
        for node in mesh.nodes:
            rendered = anim_worlds.get(node.name)
            if rendered is None:
                transforms[node.index] = Transform.identity()
            else:
                transforms[node.index] = rendered.compose(
                    bind_worlds[node.index].inverse()
                )

        def bake(vertex: Vertex,
                 transforms: dict[int, Transform] = transforms) -> Vertex:
            t = transforms[vertex.bone]
            position = t.transform_point(vertex.position)
            plan.max_bake_delta = max(
                plan.max_bake_delta,
                abs(position.x - vertex.position.x),
                abs(position.y - vertex.position.y),
                abs(position.z - vertex.position.z),
            )
            return dataclasses.replace(
                vertex,
                position=position,
                normal=t.rotate_vector(vertex.normal),
            )

        mesh.triangles = [
            dataclasses.replace(
                triangle,
                vertices=(bake(triangle.vertices[0]),
                          bake(triangle.vertices[1]),
                          bake(triangle.vertices[2])),
            )
            for triangle in mesh.triangles
        ]
    return plan


__all__ = ["WorldPlan", "bake_rendered_pose"]
