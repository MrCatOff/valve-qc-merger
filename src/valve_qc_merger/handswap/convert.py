"""End-to-end conversion of one weapon folder. Pure Python — no Blender.

    python -m handswap.convert --weapon-dir valve_original/v_ak47 \
        --out out2/v_ak47 --compile
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys

from . import asset as assetmod
from . import build as buildmod
from . import qc as qcmod
from . import smd as smdmod
from . import tuning as tuningmod
from .retarget import PEN_SLACK, build_plan, contact_seat, refine_finger_fit, seat_offsets

# per-joint limit of the automatic finger fit (grip_tuning.json may set its own)
DEFAULT_SNUG_MAX_DEG = 35.0  # corpus: mean tip error 0.183 -> 0.153 vs 18, fewer clamped joints

def _project_root() -> str:
    """Where ``storage/`` lives: the checkout, or the exe's bundle when frozen."""
    from valve_qc_merger.resources import data_root
    return str(data_root())


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
    ap.add_argument("--snug-max-deg", type=float, default=None,
                    help="per-joint clamp for the automatic snug curl "
                         "(default: the weapon's grip tuning, else %g)"
                         % DEFAULT_SNUG_MAX_DEG)
    ap.add_argument("--curl", action="append", default=[],
                    metavar="side:finger:deg",
                    help="manual extra curl per joint, e.g. "
                         "'left:ForeFinger:+8' (repeatable)")
    ap.add_argument("--grip-offset", action="append", default=[],
                    metavar="side:dx,dy,dz",
                    help="shift the palm relative to the weapon, in palm "
                         "axes (X fingers-forward, Y toward thumb, Z palm "
                         "normal), e.g. 'left:0,0,-0.4' (repeatable)")
    ap.add_argument("--weapon-aware", action="store_true", default=False,
                    help="keep the hands' skin out of the weapon: seat the "
                         "palm clear of it and fit the fingers against it "
                         "(no deeper than the original hands sat)")
    ap.add_argument("--contact-seat", action="store_true", default=False,
                    help="seat the palm so that our hand touches the weapon "
                         "where the original did (not 'glued to the palm, "
                         "fingers off it')")
    ap.add_argument("--coupled-curl", action="store_true", default=False,
                    help="the fingers' last joint follows the middle one the "
                         "way authored grips bend (not fitted on its own)")
    ap.add_argument("--one-fit-mesh", action="store_true", default=False,
                    help="old behaviour: fit the fingers to the FIRST hand "
                         "mesh only (a model with rhand + lhand meshes then "
                         "fits its left hand through fingertips, not skin)")
    ap.add_argument("--weapon-offset", default=None, metavar="dx,dy,dz",
                    help="move the weapon relative to BOTH hands, in model "
                         "space at the setup frame, e.g. '0,0.5,0'")
    return ap.parse_args(argv)


def convert(args, log=log) -> dict:
    weapon_dir = os.path.abspath(args.weapon_dir)
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    asset = assetmod.load(args.asset)
    log("CSO asset: %d bones, %d triangles"
        % (len(asset.bones), len(asset.triangles)))

    qc_path = os.path.join(weapon_dir, args.qc) if args.qc else None
    model = buildmod.load_weapon(weapon_dir, qc_path, log=log,
                                 allow_worn=getattr(args, "allow_worn", False))

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
    # explicit --grip-offset / --weapon-offset flags override it. Lives next
    # to the CSO asset (storage/handswap/grip_tuning.json).
    tuned = tuningmod.load_entry(tuningmod.tuning_path(args.asset),
                                 os.path.basename(weapon_dir))
    grip_offsets = dict(tuned.get("grip_offset", {}))
    if tuned:
        log("Grip tuning: %s" % tuned)
    for spec in args.grip_offset:
        side, _, xyz = spec.partition(":")
        grip_offsets[side.strip().lower()] = \
            [float(x) for x in xyz.split(",")]

    weapon_offset = getattr(args, "weapon_offset", None)
    if isinstance(weapon_offset, str):
        weapon_offset = [float(x) for x in weapon_offset.split(",")]
    if not weapon_offset or not any(weapon_offset):
        weapon_offset = tuned.get("weapon_offset")
    if weapon_offset is not None:
        if len(weapon_offset) != 3:
            raise RuntimeError("weapon offset needs 3 numbers: dx,dy,dz")
        log("Weapon offset: %s" % list(weapon_offset))

    log("Retarget plan (setup: %r frame 0):" % first_seq)
    # skin centroids of the original hands at the grip: the finger fit
    # compares skin, not two rigs' joints. One hand mesh only: male and
    # female variants differ in size and our asset hand is the male one.
    # (Seating the PALM from centroids, or by ICP onto the original palm skin,
    # was measured worse on the corpus benchmark: the joint-based seat stays.)
    fit_meshes = [m for m in dropped if "female" not in m.lower()] or dropped
    if getattr(args, "one_fit_mesh", False):
        fit_meshes = fit_meshes[:1]
    else:
        # one mesh per HAND, not one mesh: stock Valve models carry rhand and
        # lhand as two meshes, and taking only the first left the other hand
        # (the gripping one) without any skin to fit to. A second variant of
        # the same hand (it skins the same finger bones) is still skipped.
        fingers = {b for h in model.hands for ch in h.chains for b in ch}
        chosen, covered = [], set()
        for m in fit_meshes:
            own = {b for b, w in model.weights.get(m, {}).items() if w > 0} & fingers
            if own & covered:
                continue
            chosen.append(m)
            covered |= own
        fit_meshes = chosen
    centroids = buildmod.skin_centroids(model, fit_meshes, setup_world)
    old_skin = buildmod.skin_triangles(model, fit_meshes, setup_world)

    # the weapon as a collider: how deep the ORIGINAL hands sat is what ours may
    weapon = allowed = None
    if getattr(args, "weapon_aware", False):
        from .contact import surface_samples
        weapon = buildmod.weapon_shell(model, kept, setup_world, skip_bones=hand_bones)
        old_depth = {b: float(weapon.depth(surface_samples(t)).max())
                     for b, t in old_skin.items()}
        log("Weapon-aware fit: %d weapon triangles; original hands sit up to "
            "%.2f u inside" % (len(weapon), max(old_depth.values(), default=0.0)))
    plan = build_plan(asset, model.skel, model.hands, setup_world, log=log,
                      grip_offsets=grip_offsets, weapon_offset=weapon_offset)
    if not plan.sides:
        raise RuntimeError("no hand could be retargeted")
    if getattr(args, "contact_seat", False):
        shell = weapon if weapon is not None else buildmod.weapon_shell(
            model, kept, setup_world, skip_bones=hand_bones)
        seats = contact_seat(plan, setup_world, shell, old_skin, log=log)
        if seats:
            for side, shift in seats.items():
                base = grip_offsets.get(side, [0.0, 0.0, 0.0])
                grip_offsets[side] = [float(a) + float(b) for a, b in zip(base, shift, strict=True)]
            plan = build_plan(asset, model.skel, model.hands, setup_world, log=log,
                              grip_offsets=grip_offsets, weapon_offset=weapon_offset)
    if weapon is not None:
        palm_allowed = {sp.side: PEN_SLACK + max(
            (old_depth.get(b, 0.0) for b in (sp.hand.wrist, sp.hand.fan)), default=0.0)
            for sp in plan.sides}
        seats = seat_offsets(plan, setup_world, weapon, palm_allowed, log=log)
        if seats:
            for side, shift in seats.items():
                base = grip_offsets.get(side, [0.0, 0.0, 0.0])
                grip_offsets[side] = [float(a) + float(b) for a, b in zip(base, shift, strict=True)]
            plan = build_plan(asset, model.skel, model.hands, setup_world, log=log,
                              grip_offsets=grip_offsets, weapon_offset=weapon_offset)
        allowed = {}
        for sp in plan.sides:
            for old_chain, cso_chain in sp.pairs:
                for o, c in zip(old_chain, cso_chain, strict=False):
                    allowed[c] = PEN_SLACK + old_depth.get(o, 0.0)

    # finger curls: the tuning table first, explicit --curl per finger wins
    manual = {}
    for spec in tuningmod.curl_specs(tuned.get("curl", {})) + list(args.curl):
        side, finger, deg = spec.split(":")
        manual[(side.strip().lower(), finger.strip())] = float(deg)
    max_deg = getattr(args, "snug_max_deg", None)
    if max_deg is None:
        max_deg = tuned.get("snug_max_deg", DEFAULT_SNUG_MAX_DEG)
    if args.snug or manual:
        refine_finger_fit(plan, model.skel, setup_world,
                          max_deg=max_deg if args.snug else 0.0,
                          manual=manual, log=log,
                          old_centroids=centroids, old_skin=old_skin,
                          weapon=weapon, allowed=allowed,
                          coupled=getattr(args, "coupled_curl", False))

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
    if asset.render_modes:  # an asset with its own textures (zombie hands)
        lines = "".join(f'$texrendermode "{mat}" {mode}\n'
                        for mat, mode in sorted(asset.render_modes.items()))
        anchor = re.search(r"^\s*\$(?:body|bodygroup)\b", qc_text, re.MULTILINE)
        at = anchor.start() if anchor else len(qc_text)
        qc_text = qc_text[:at] + lines + qc_text[at:]
    qc_name = os.path.basename(model.qc.path)
    with open(os.path.join(out_dir, qc_name), "w", encoding="latin-1",
              newline="\n") as f:
        f.write(qc_text)
    log("Wrote %s" % qc_name)

    # textures
    # decompilers put them next to the QC (Crowbar) or under maps_8bit/
    # (decompmdl, our own); the output is flat. The top level wins a clash.
    n_bmp = 0
    copied = set()
    for root, _dirs, files in sorted(os.walk(weapon_dir), key=lambda w: len(w[0])):
        if os.path.abspath(root).startswith(os.path.abspath(out_dir)):
            continue
        for f in sorted(files):
            if f.lower().endswith(".bmp") and f.lower() not in copied:
                shutil.copy2(os.path.join(root, f), os.path.join(out_dir, f))
                copied.add(f.lower())
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
    for mat, path in sorted(asset.textures.items()):  # the hands' own textures
        if os.path.isfile(path):
            shutil.copy2(path, os.path.join(out_dir, mat))
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
