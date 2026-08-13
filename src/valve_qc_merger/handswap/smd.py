"""SMD parsing/writing + skeleton FK (pure Python / numpy).

An SMD file:
  version 1
  nodes                      <id> "<name>" <parent_id>
  skeleton                   time <n> / <id> px py pz rx ry rz
  [triangles]                <material>\n 3 x vertex lines
Vertex line (GoldSrc): <bone_id> px py pz nx ny nz u v [nlinks (id w)...]
Positions are model space at bind pose; skeleton keys are parent-local
(root bones: model space). Bones match across files BY NAME.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

from .math3d import inv_rigid, local_matrix


@dataclass
class Node:
    id: int
    name: str
    parent: int  # -1 for root


@dataclass
class Vertex:
    bone: int
    pos: np.ndarray
    normal: np.ndarray
    uv: tuple[float, float]
    links: list[tuple[int, float]] = field(default_factory=list)
    # links empty -> fully rigid to `bone`

    def dominant_bone(self) -> int:
        if not self.links:
            return self.bone
        return max(self.links, key=lambda p: p[1])[0]

    def weights(self) -> list[tuple[int, float]]:
        """Normalized (bone, weight) list; rigid remainder goes to `bone`."""
        if not self.links:
            return [(self.bone, 1.0)]
        total = sum(w for _, w in self.links)
        if total < 1.0 - 1e-4:
            out = list(self.links) + [(self.bone, 1.0 - total)]
        else:
            out = [(b, w / total) for b, w in self.links]
        return out


@dataclass
class Triangle:
    material: str
    verts: list[Vertex]


@dataclass
class Smd:
    nodes: list[Node]
    # frames[t][bone_id] = (pos(3,), rot(3,)); every node present each frame
    frames: list[dict[int, tuple[np.ndarray, np.ndarray]]]
    triangles: list[Triangle]
    path: str = ""

    # -- helpers ----------------------------------------------------------
    def name_of(self) -> dict[int, str]:
        return {n.id: n.name for n in self.nodes}

    def id_of(self) -> dict[str, int]:
        return {n.name: n.id for n in self.nodes}

    def parent_of(self) -> dict[int, int]:
        return {n.id: n.parent for n in self.nodes}

    def world_matrices(self, frame: int) -> dict[int, np.ndarray]:
        """FK: id -> 4x4 world matrix at the given frame."""
        keys = self.frames[frame]
        parents = self.parent_of()
        out: dict[int, np.ndarray] = {}

        def build(i: int) -> np.ndarray:
            if i in out:
                return out[i]
            pos, rot = keys[i]
            m = local_matrix(pos, rot)
            p = parents[i]
            if p >= 0:
                m = build(p) @ m
            out[i] = m
            return m

        for n in self.nodes:
            build(n.id)
        return out

    def bind_matrices(self) -> dict[int, np.ndarray]:
        return self.world_matrices(0)

    def skin(self, world: dict[int, np.ndarray],
             bind_inv: dict[int, np.ndarray] | None = None) -> np.ndarray:
        """GoldSrc skinning of the triangles: (n_tris*3, 3) positions."""
        if bind_inv is None:
            bind_inv = {i: inv_rigid(m) for i, m in self.bind_matrices().items()}
        out = np.empty((len(self.triangles) * 3, 3))
        k = 0
        for tri in self.triangles:
            for v in tri.verts:
                p = np.zeros(3)
                for b, w in v.weights():
                    m = world[b] @ bind_inv[b]
                    p += w * (m[:3, :3] @ v.pos + m[:3, 3])
                out[k] = p
                k += 1
        return out


def parse(path: str) -> Smd:
    nodes: list[Node] = []
    frames: list[dict] = []
    triangles: list[Triangle] = []
    section = None
    cur: dict | None = None
    material = None
    tri_verts: list[Vertex] = []

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            # NOTE: '#' is NOT a comment — GoldSrc texture names may start
            # with it ('#512512CSO_Girl_Hand_long.bmp')
            if not line or line.startswith("//") or line.startswith(";"):
                continue
            low = line.lower()
            if low.startswith("version"):
                continue
            if low == "nodes":
                section = "nodes"
                continue
            if low == "skeleton":
                section = "skeleton"
                continue
            if low == "triangles":
                section = "triangles"
                continue
            if low == "end":
                if section == "skeleton" and cur is not None:
                    frames.append(cur)
                    cur = None
                section = None
                continue

            if section == "nodes":
                # <id> "name with spaces" <parent>
                first, rest = line.split(None, 1)
                if rest.startswith('"'):
                    close = rest.rindex('"')
                    name = rest[1:close]
                    parent = int(rest[close + 1:].split()[0])
                else:
                    parts = rest.rsplit(None, 1)
                    name, parent = parts[0], int(parts[1])
                nodes.append(Node(int(first), name, parent))

            elif section == "skeleton":
                if low.startswith("time"):
                    if cur is not None:
                        frames.append(cur)
                    # SMD semantics: unkeyed bones repeat the previous frame
                    cur = dict(frames[-1]) if frames else {}
                    continue
                p = line.split()
                cur[int(p[0])] = (np.array([float(p[1]), float(p[2]),
                                            float(p[3])]),
                                  np.array([float(p[4]), float(p[5]),
                                            float(p[6])]))

            elif section == "triangles":
                p = line.split()
                is_vertex = len(p) >= 9
                if is_vertex:
                    try:
                        int(p[0])
                        [float(x) for x in p[1:9]]
                    except ValueError:
                        is_vertex = False
                if not is_vertex:
                    material = line
                    continue
                links = []
                if len(p) > 9:
                    n_links = int(p[9])
                    for i in range(n_links):
                        links.append((int(p[10 + 2 * i]),
                                      float(p[11 + 2 * i])))
                tri_verts.append(Vertex(
                    bone=int(p[0]),
                    pos=np.array([float(p[1]), float(p[2]), float(p[3])]),
                    normal=np.array([float(p[4]), float(p[5]), float(p[6])]),
                    uv=(float(p[7]), float(p[8])),
                    links=links))
                if len(tri_verts) == 3:
                    triangles.append(Triangle(material, tri_verts))
                    tri_verts = []

    if cur is not None:
        frames.append(cur)
    smd = Smd(nodes, frames, triangles, path=os.path.abspath(path))
    _validate(smd)
    return smd


def _validate(smd: Smd) -> None:
    ids = {n.id for n in smd.nodes}
    for n in smd.nodes:
        if n.parent >= 0 and n.parent not in ids:
            raise ValueError("%s: node %d parent %d missing"
                             % (smd.path, n.id, n.parent))
    for t, fr in enumerate(smd.frames):
        missing = ids - set(fr)
        if missing:
            raise ValueError("%s: frame %d missing bones %s"
                             % (smd.path, t, sorted(missing)))


def write(path: str, smd: Smd) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("version 1\n")
        f.write("nodes\n")
        for n in smd.nodes:
            f.write('%d "%s" %d\n' % (n.id, n.name, n.parent))
        f.write("end\n")
        f.write("skeleton\n")
        for t, fr in enumerate(smd.frames):
            f.write("time %d\n" % t)
            for n in smd.nodes:
                pos, rot = fr[n.id]
                f.write("%d  %.6f %.6f %.6f  %.6f %.6f %.6f\n"
                        % (n.id, pos[0], pos[1], pos[2],
                           rot[0], rot[1], rot[2]))
        f.write("end\n")
        if smd.triangles:
            f.write("triangles\n")
            for tri in smd.triangles:
                f.write("%s\n" % (tri.material or "default.bmp"))
                for v in tri.verts:
                    f.write("%d  %.6f %.6f %.6f  %.6f %.6f %.6f  %.6f %.6f\n"
                            % (v.bone, v.pos[0], v.pos[1], v.pos[2],
                               v.normal[0], v.normal[1], v.normal[2],
                               v.uv[0], v.uv[1]))
            f.write("end\n")
