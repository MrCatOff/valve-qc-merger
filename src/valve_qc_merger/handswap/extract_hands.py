"""One-time extractor: CSO hands rig (.blend) -> assets/cso_hands.json.gz

Reads the armature and mesh with the raw bpy API (no Blender Source Tools —
their importer/exporter quirks were the root cause of most past bugs) and
writes a self-contained asset the pure-Python converter consumes:

  - every deform bone: name, parent, rest matrix (armature space, 4x4)
  - the hands mesh, triangulated: per-corner position/normal/uv +
    normalized bone weights, in armature space (bind pose)

Run:
  /Applications/Blender.app/Contents/MacOS/Blender -b \
      hands_base_2_0/hands_base_2.0.blend -P extract_hands.py
"""
import gzip
import json
import os
import sys

import bpy

ARMATURE = "CSO Hands"
MESH = "hands"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "assets", "cso_hands.json.gz")


def main():
    arm = bpy.data.objects[ARMATURE]
    mesh_ob = bpy.data.objects[MESH]
    me = mesh_ob.data

    # bones that actually receive vertex weights (plus ancestors) survive
    group_names = {vg.name for vg in mesh_ob.vertex_groups}
    weighted = set()
    idx2name = {i: vg.name for i, vg in enumerate(mesh_ob.vertex_groups)}
    for v in me.vertices:
        for g in v.groups:
            if g.weight > 1e-4:
                weighted.add(idx2name[g.group])
    keep = set()
    for name in weighted:
        b = arm.data.bones.get(name)
        while b and b.name not in keep:
            keep.add(b.name)
            b = b.parent

    bones = []
    for b in arm.data.bones:
        if b.name not in keep:
            continue
        m = arm.matrix_world @ b.matrix_local
        bones.append({
            "name": b.name,
            "parent": b.parent.name if (b.parent and b.parent.name in keep)
                      else None,
            "rest_world": [list(row) for row in m],
            "length": b.length,
        })

    # mesh in armature space at bind pose
    to_arm = arm.matrix_world.inverted() @ mesh_ob.matrix_world
    nrm_m = to_arm.to_3x3().inverted().transposed()
    me.calc_loop_triangles()
    uv = me.uv_layers.active.data
    materials = [m.name if m else "default.bmp" for m in me.materials] \
        or ["default.bmp"]

    tris = []
    for lt in me.loop_triangles:
        corners = []
        for li in lt.loops:
            vi = me.loops[li].vertex_index
            v = me.vertices[vi]
            co = to_arm @ v.co
            n = (nrm_m @ me.corner_normals[li].vector).normalized()
            w = [(idx2name[g.group], g.weight) for g in v.groups
                 if g.weight > 1e-4 and idx2name[g.group] in keep]
            total = sum(x for _, x in w) or 1.0
            w = sorted(((n_, x / total) for n_, x in w),
                       key=lambda p: -p[1])
            corners.append({
                "pos": list(co),
                "normal": list(n),
                "uv": list(uv[li].uv),
                "weights": w,
            })
        tris.append({"mat": materials[lt.material_index], "corners": corners})

    asset = {
        "source_blend": os.path.basename(bpy.data.filepath),
        "bones": bones,
        "triangles": tris,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as f:
        json.dump(asset, f)
    print("EXTRACT_OK bones=%d tris=%d -> %s" % (len(bones), len(tris), OUT))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
