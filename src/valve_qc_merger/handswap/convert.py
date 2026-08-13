"""End-to-end conversion of one weapon folder. Pure Python — no Blender.

    python -m handswap.convert --weapon-dir valve_original/v_ak47 \
        --out out2/v_ak47 --compile
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys

from . import asset as assetmod
from . import build as buildmod
from . import qc as qcmod
from . import smd as smdmod
from .retarget import build_plan, refine_finger_fit

def _project_root() -> str:
    """Walk up from this module to the valve-qc-merger checkout (the dir
    holding pyproject.toml). Falls back to the cwd for frozen/wheel runs."""
    d = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.isfile(os.path.join(d, "pyproject.toml")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.getcwd()
        d = parent


REPO = _project_root()
# Default hands texture: the project's shared male CSO hands skin.
DEFAULT_HANDS_BMP = os.path.join(REPO, "storage", "hands", "male.bmp")
DEFAULT_STUDIOMDL = os.path.join(REPO, "tmp", "studiomdl")


def log(msg):
    print("[handswap] %s" % msg, flush=True)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--weapon-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--qc", default=None, help="QC file (default: the only "
                    ".qc in weapon-dir)")
    ap.add_argument("--asset", default=assetmod.DEFAULT_ASSET,
                    help="CSO hands asset (from extract_hands.py)")
    ap.add_argument("--hands-texture", default=None,
                    help="BMP copied to <out>/hands.bmp "
                         "(default: classic CSO male long)")
    ap.add_argument("--modelname", default=None,
                    help="override $modelname")
    ap.add_argument("--studiomdl", default=DEFAULT_STUDIOMDL)
    ap.add_argument("--compile", action="store_true")
    ap.add_argument("--verify", action="store_true", default=True)
    ap.add_argument("--no-verify", dest="verify", action="store_false")
    ap.add_argument("--snug", action="store_true", default=True,
                    help="curl fingers until they touch the weapon/other "
                         "hand (on by default)")
    ap.add_argument("--no-snug", dest="snug", action="store_false")
    ap.add_argument("--snug-max-deg", type=float, default=18.0,
                    help="per-joint clamp for the automatic snug curl")
    ap.add_argument("--curl", action="append", default=[],
                    metavar="side:finger:deg",
                    help="manual extra curl per joint, e.g. "
                         "'left:ForeFinger:+8' (repeatable)")
    ap.add_argument("--grip-offset", action="append", default=[],
                    metavar="side:dx,dy,dz",
                    help="shift the palm relative to the weapon, in palm "
                         "axes (X fingers-forward, Y toward thumb, Z palm "
                         "normal), e.g. 'left:0,0,-0.4' (repeatable)")
    return ap.parse_args(argv)


def convert(args) -> dict:
    weapon_dir = os.path.abspath(args.weapon_dir)
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    asset = assetmod.load(args.asset)
    log("CSO asset: %d bones, %d triangles"
        % (len(asset.bones), len(asset.triangles)))

    qc_path = os.path.join(weapon_dir, args.qc) if args.qc else None
    model = buildmod.load_weapon(weapon_dir, qc_path, log=log)

    # bones / meshes owned by the original hands
    hand_bones = buildmod.hand_bone_set(model, log=log)
    hand_mats = buildmod.hand_materials(model, hand_bones)
    model._hand_mats = hand_mats
    dropped, kept = buildmod.classify_meshes(model, hand_bones, hand_mats,
                                             log=log)
    if not dropped and all(
            not (set(w) & hand_bones) for w in model.weights.values()):
        raise RuntimeError("hand bones found but no geometry uses them")
    if not kept and not any(
            set(w) - hand_bones for w in model.weights.values()):
        raise RuntimeError("no weapon geometry would survive — hand "
                           "detection ate the whole model")
    model._hand_bones = hand_bones
    model._dropped = dropped
    model._kept = kept

    # setup state: frame 0 of the first sequence = the in-game grip
    first_seq = model.qc.sequences[0]["smd"]
    setup_world = buildmod.anim_world_frames(model.anims[first_seq],
                                             model.skel)[0]

    # per-weapon tuning table (committed, calibrated against examples/);
    # explicit --grip-offset flags override it. Lives next to the CSO asset
    # (storage/handswap/grip_tuning.json).
    grip_offsets = {}
    arm_dirs = {}
    tuning_path = os.path.join(os.path.dirname(os.path.abspath(
        args.asset)), "grip_tuning.json")
    if os.path.isfile(tuning_path):
        with open(tuning_path) as f:
            tuning = json.load(f).get(os.path.basename(weapon_dir), {})
        grip_offsets.update(tuning.get("grip_offset", {}))
        if grip_offsets:
            log("Grip tuning: %s" % grip_offsets)
        arm_dirs.update(tuning.get("arm_dir", {}))
    for spec in args.grip_offset:
        side, _, xyz = spec.partition(":")
        grip_offsets[side.strip().lower()] = \
            [float(x) for x in xyz.split(",")]
    for spec in getattr(args, "arm_dir", []) or []:
        side, _, xyz = spec.partition(":")
        arm_dirs[side.strip().lower()] = [float(x) for x in xyz.split(",")]

    log("Retarget plan (setup: %r frame 0):" % first_seq)
    plan = build_plan(asset, model.skel, model.hands, setup_world, log=log,
                      grip_offsets=grip_offsets, arm_dirs=arm_dirs)
    if not plan.sides:
        raise RuntimeError("no hand could be retargeted")

    if args.snug or args.curl:
        manual = {}
        for spec in args.curl:
            side, finger, deg = spec.split(":")
            manual[(side.strip().lower(), finger.strip())] = float(deg)
        refine_finger_fit(plan, model.skel, setup_world,
                          max_deg=args.snug_max_deg if args.snug else 0.0,
                          manual=manual, log=log)

    asm = buildmod.assemble(model, plan, setup_world, log=log)
    log("Merged skeleton: %d CSO + %d weapon bones (%d original removed)"
        % (len(asm.cso_bones), len(asm.kept_weapon), len(asm.deleted)))

    # references
    out_refs = {"hands": buildmod.build_hands_reference(asm, model, plan)}
    for mesh in kept:
        out_refs[mesh] = buildmod.rebuild_weapon_reference(
            asm, model, plan, mesh, log=log)
    for name, smd in out_refs.items():
        smdmod.write(os.path.join(out_dir, name + ".smd"), smd)
    log("Wrote %d reference SMDs (hands: %d triangles)"
        % (len(out_refs), len(out_refs["hands"].triangles)))

    # sequences
    for rel, anim in model.anims.items():
        out = buildmod.build_sequence(asm, model, plan, anim)
        path = os.path.join(out_dir, rel + ".smd")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        smdmod.write(path, out)
    log("Wrote %d sequence SMDs" % len(model.anims))

    # QC
    survivors = buildmod.compute_survivors(asm, out_refs)
    fixes = buildmod.attachment_fixes(asm, model, plan, log=log)
    drop_studios = {d.lower() for d in dropped}
    qc_text = qcmod.rewrite(model.qc, drop_studios=drop_studios,
                            attachment_fixes=fixes, survivors=survivors,
                            modelname=args.modelname)
    qc_name = os.path.basename(model.qc.path)
    with open(os.path.join(out_dir, qc_name), "w", encoding="utf-8",
              newline="\n") as f:
        f.write(qc_text)
    log("Wrote %s" % qc_name)

    # textures
    n_bmp = 0
    for f in os.listdir(weapon_dir):
        if f.lower().endswith(".bmp"):
            shutil.copy2(os.path.join(weapon_dir, f),
                         os.path.join(out_dir, f))
            n_bmp += 1
    hands_bmp = args.hands_texture or DEFAULT_HANDS_BMP
    if not os.path.isfile(hands_bmp):
        cands = glob.glob(os.path.join(REPO, "storage", "hands", "*.bmp"))
        if not cands:
            raise FileNotFoundError(
                "no hands texture found (looked for %s and storage/hands/*.bmp)"
                % hands_bmp)
        hands_bmp = sorted(cands)[0]
    shutil.copy2(hands_bmp, os.path.join(out_dir, "hands.bmp"))
    log("Copied %d weapon BMPs + hands.bmp (%s)"
        % (n_bmp, os.path.basename(hands_bmp)))

    info = {
        "weapon": os.path.basename(weapon_dir),
        "sides": [{
            "side": sp.side,
            "old_wrist": sp.hand.wrist,
            "fit_cost": sp.fit_cost,
            "pairs": [[o[0], c[0]] for o, c in sp.pairs],
        } for sp in plan.sides],
        "dropped_meshes": dropped,
        "kept_meshes": kept,
        "removed_bones": sorted(asm.deleted),
        "cso_bones": asm.cso_bones,
        "kept_weapon_bones": asm.kept_weapon,
    }
    with open(os.path.join(out_dir, "conversion_info.json"), "w") as f:
        json.dump(info, f, indent=1)

    # verify BEFORE compiling — a broken model must not silently ship
    if args.verify:
        from . import verify as verifymod
        report = verifymod.verify(weapon_dir, out_dir, model, plan, asm,
                                  log=log)
        info["verify"] = report
        with open(os.path.join(out_dir, "conversion_info.json"), "w") as f:
            json.dump(info, f, indent=1)
        if not report["ok"]:
            raise RuntimeError("verification FAILED: %s"
                               % "; ".join(report["errors"]))

    if args.compile:
        cmd = [os.path.abspath(args.studiomdl), qc_name]
        log("Compiling: %s" % " ".join(cmd))
        r = subprocess.run(cmd, cwd=out_dir, capture_output=True, text=True)
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-12:])
        if r.returncode != 0:
            raise RuntimeError("studiomdl failed (%d):\n%s"
                               % (r.returncode, tail))
        log("studiomdl OK:\n%s" % tail)
    return info


def main(argv=None):
    args = parse_args(argv)
    try:
        convert(args)
    except Exception as e:
        log("ERROR: %s" % e)
        raise
    log("DONE")


if __name__ == "__main__":
    main()
