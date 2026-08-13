"""Debug renderer: skin the converted model (or the original) at chosen
frames and save PNGs from the player's viewpoint. Visual regression tool —
twisted fingers and crumpled palms are obvious here long before in-game.

    python -m handswap.render out2/v_ak47 --frames idle1:0 reload:20
    python -m handswap.render valve_original/v_ak47 --original
"""
from __future__ import annotations

import argparse
import glob
import os

import numpy as np

from . import qc as qcmod
from . import smd as smdmod
from .math3d import inv_rigid

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402
from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: E402


def _load(model_dir: str):
    qcs = glob.glob(os.path.join(model_dir, "*.qc"))
    qc = qcmod.parse(qcs[0])
    refs = []
    seen = set()
    for _g, studio in qc.references:
        base = studio.replace("\\", "/").split("/")[-1]
        if base in seen:
            continue
        seen.add(base)
        p = os.path.join(model_dir, base + ".smd")
        if os.path.isfile(p):
            refs.append((base, smdmod.parse(p)))
    anims = {}
    for seq in qc.sequences:
        p = os.path.join(model_dir, seq["smd"] + ".smd")
        if seq["name"] not in anims and os.path.isfile(p):
            anims[seq["name"]] = smdmod.parse(p)
    return qc, refs, anims


def _skin(ref: smdmod.Smd, world_by_name: dict[str, np.ndarray]):
    n_of = ref.name_of()
    bind_inv = {i: inv_rigid(m) for i, m in ref.bind_matrices().items()}
    tris = []
    for tri in ref.triangles:
        pts = []
        for v in tri.verts:
            b = v.dominant_bone()
            m = world_by_name[n_of[b]] @ bind_inv[b]
            pts.append(m[:3, :3] @ v.pos + m[:3, 3])
        tris.append(pts)
    return np.array(tris)


def render_frame(refs, world_by_name, out_png: str, title: str = "",
                 center=None, radius: float | None = None):
    fig = plt.figure(figsize=(12, 7))
    views = [("player view", (-15, -90)), ("top", (80, -90)),
             ("side", (0, 0))]
    all_tris = [(_skin(smd, world_by_name), name) for name, smd in refs]
    for k, (vname, (elev, azim)) in enumerate(views):
        ax = fig.add_subplot(1, 3, k + 1, projection="3d")
        for tris, name in all_tris:
            if not len(tris):
                continue
            col = "#c9a37e" if name == "hands" else "#8899aa"
            pc = Poly3DCollection(tris, facecolors=col, shade=True,
                                  lightsource=matplotlib.colors.LightSource(
                                      azdeg=200, altdeg=45))
            ax.add_collection3d(pc)
        pts = np.concatenate([t.reshape(-1, 3) for t, _ in all_tris
                              if len(t)])
        c = np.asarray(center) if center is not None else pts.mean(axis=0)
        r = radius if radius is not None \
            else float(np.abs(pts - c).max()) * 0.7
        ax.set_xlim(c[0] - r, c[0] + r)
        ax.set_ylim(c[1] - r, c[1] + r)
        ax.set_zlim(c[2] - r, c[2] + r)
        ax.view_init(elev=elev, azim=azim)
        ax.set_title("%s %s" % (title, vname), fontsize=9)
        ax.set_axis_off()
    fig.tight_layout()
    fig.savefig(out_png, dpi=110)
    plt.close(fig)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("model_dir")
    ap.add_argument("--frames", nargs="*", default=None,
                    help="seq:frame pairs (default: frame 0 + middle of "
                         "every sequence)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--focus", default=None,
                    help="bone name to center the view on (close-up)")
    ap.add_argument("--radius", type=float, default=9.0,
                    help="view radius with --focus")
    args = ap.parse_args(argv)

    qc, refs, anims = _load(args.model_dir)
    out_dir = args.out or os.path.join(args.model_dir, "renders")
    os.makedirs(out_dir, exist_ok=True)

    wanted = []
    if args.frames:
        for spec in args.frames:
            # rpartition: sequence names may contain colons themselves
            seq, _, fr = spec.rpartition(":")
            if not seq:
                seq, fr = fr, "0"
            wanted.append((seq, int(fr or 0)))
    else:
        for name, smd in anims.items():
            wanted.append((name, 0))
            if len(smd.frames) > 2:
                wanted.append((name, len(smd.frames) // 2))

    for seq_name, frame in wanted:
        smd = anims.get(seq_name)
        if smd is None or frame >= len(smd.frames):
            print("skip %s:%d" % (seq_name, frame))
            continue
        n_of = smd.name_of()
        world = {n_of[i]: m for i, m in smd.world_matrices(frame).items()}
        # bones present in refs but not the sequence: bind fallback
        for _name, ref in refs:
            rn = ref.name_of()
            bw = ref.world_matrices(0)
            for i, nm in rn.items():
                world.setdefault(nm, bw[i])
        center = rad = None
        if args.focus:
            if args.focus not in world:
                print("focus bone %r not found" % args.focus)
            else:
                center = world[args.focus][:3, 3]
                rad = args.radius
        png = os.path.join(out_dir, "%s_%03d.png" % (seq_name, frame))
        render_frame(refs, world, png, "%s:%d" % (seq_name, frame),
                     center=center, radius=rad)
        print("wrote", png)


if __name__ == "__main__":
    main()
