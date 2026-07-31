# `retarget` — retarget a weapon's animations onto the reference hands

Takes a GoldSource viewmodel (weapon mesh + its original hands + animation set)
and rebuilds it on the project's reference hands, producing a directory that
compiles with `studiomdl` as-is. Driven by headless Blender (one worker process
per sequence) with the Blender Source Tools add-on; everything the workers emit
is re-verified at text level before the run reports success.

## Quick start

```bash
# Everything discovered from the weapon's QC:
python -m valve_qc_merger retarget --weapon-dir tmp/usp --out tmp/final_usp

# Explicit animation glob (when there is no QC, or it is wrong):
python -m valve_qc_merger retarget \
    --weapon-dir tmp/pistols/view/v_elite \
    --anims 'v_elite_anims/*.smd' \
    --out tmp/final_v_elite
```

## What ends up in `--out`

| File | Content |
|---|---|
| `<weapon>.qc` | GoldSource-ready QC: `$bodygroup "weapon"` + `$bodygroup "hands"`, original sequences (fps + events), surviving `$attachment` lines |
| `<weapon>.smd` | Weapon-only mesh on the unified skeleton |
| `hands_<variant>.smd` | One mesh per hand variant (default `hands_female`, `hands_male`), all sharing the same skeleton and node table |
| `anims/<sequence>.smd` | Every retargeted sequence |
| `*.bmp` | Every referenced texture, staged next to the QC, validated 8-bit |
| `report.json` | Full run record: per-sequence worker reports, derived hand offset, verify outcome |

All emitted SMDs use the classic exporter indentation (two-space node/vertex
lines, `  time N`, four-space pose lines) — the layout GoldSource `studiomdl`
is known to compile. To build: copy nothing, just run `studiomdl <weapon>.qc`
inside the output directory.

## Inputs and how they are discovered

The weapon's QC (found in `--weapon-dir`) is the manifest:

- `$bodygroup "weapon" { studio "..." }` → the weapon mesh SMD,
- `$bodygroup "hands" { studio "..." ... }` → the original hand meshes; the
  first entry is used as the grip-contact ground truth,
- each `$sequence "name" { "dir\name" ... }` → an animation SMD (backslash
  paths and missing `.smd` extensions are handled).

Overrides: `--weapon-pv`, `--original-hands`, `--anims <glob>`. Without a QC,
the weapon mesh falls back to `*-PV.smd` and the hands to `f_*_hand_Low.smd`
globs, and `--anims` becomes required.

The reference hands default to `storage/hands/reference_hands.smd`; the hand
bodygroup variants default to `storage/hands/{female,male}.smd`. Both can be
changed in the config (see `configs/example_retarget.toml`) or per-run with
`--reference` / `--hands NAME=PATH` (repeatable; `--hands blank` disables
bodygroups and merges the reference hands with the weapon into a single SMD).

## Options

| Flag | Meaning |
|---|---|
| `--weapon-dir DIR` | weapon directory (required) |
| `--out DIR` | output directory (required) |
| `--anims GLOB` | animation selection; default: the QC's `$sequence` paths |
| `--reference SMD` | reference hands; default from config |
| `--hands NAME=PATH` | add/override a hand bodygroup variant (repeatable); `blank` disables |
| `--weapon-pv SMD` | override the weapon mesh |
| `--original-hands SMD` | override the contact ground-truth hand mesh |
| `--config TOML` | config file; omitted keys take defaults |
| `--sequences a,b` | run a subset of sequences |
| `--dry-run` | import + rig discovery + correspondence only; writes the bone map, no export |
| `--no-export` | retarget but skip skeleton unification/SMD export |
| `--blender PATH` | Blender executable (else `VQM_BLENDER`, `PATH`, then the macOS default) |
| `--jobs N` | reserved (workers currently run sequentially) |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | every sequence exported and the verification gate passed |
| 2 | a sequence failed, or the exported model failed the gate |
| 3 | input/rig-discovery/correspondence failure (bad paths, unmappable rig) |
| 4 | environment/assertion failure (Blender missing, §5 node-table gate, worker crash) |

## How it works (pipeline)

1. **Text gates before any Blender runs** — the weapon, original hands and all
   animations must share one node table; every hand variant must share the
   reference skeleton exactly (names, parents, rest transforms).
2. **Import & consolidate** (per sequence, in a fresh headless Blender) — the
   animation attaches to the weapon rig; the original hand mesh is rebound to
   it as the posed ground truth; hand variants are rebound to the reference rig.
3. **Rig discovery & correspondence** — arms, wrists, and finger chains are
   found geometrically (weights + structure; bone names are never required, but
   L/R names decide arm pairing when both rigs carry them). Thumbs are
   identified by abduction of real joint positions, cross-checked against the
   knuckle plane. Reversed-hierarchy rigs (forearm as a child of the hand,
   helper/bullet stubs under the wrist) are handled.
4. **Hand placement** — each wrist anchors at its source wrist, shifted back
   along the source grip's palm-forward axis by `hand_center_fraction` (default
   0.5) of the measured hand-length difference: the two hands' centres align,
   so the size surplus splits evenly behind and ahead of the grip. The derived
   vector is recorded in the report (`hand_offset_auto`); an explicit
   `hand_offset` overrides it.
5. **Pose transfer** — rotation retargeting through per-arm anatomical frames;
   fingers copy the original grip by direction transfer (each joint's segment
   is aimed exactly where the source finger's segment points — no IK, nothing
   to break). Only one bone per arm ever receives translation; the reference
   hands are never renamed, reparented, rescaled or reshaped.
6. **Weapon placement** — zero offset: the weapon stays exactly where its own
   animation puts it (attachments, dual-gun sync and screen framing preserved).
7. **Unify & export** — the gun subtree is appended under the correct wrist on
   the reference skeleton, the weapon animation is transferred verbatim, and
   the mesh/animation SMDs are exported and normalised to the compile-proven
   indented layout. Euler tracks are unwrapped in place for continuity.
8. **Verification gate (12 checks)** — parses the emitted text and proves:
   node tables identical across every mesh and animation; reference bones,
   rest transforms and mesh geometry unchanged vs the input reference; hand
   translations frozen (bar the per-arm anchor); no NaN/Inf; rotation
   continuity (authored cuts in the source are recognised and excused);
   frame counts/indices match the source; every weapon bone's world pose equals
   the source's per frame; all referenced textures exist as ASCII-named,
   space-free 8-bit BMPs (spaces and missing `.bmp` extensions are fixed
   automatically, in both the SMD and the staged file).

## Troubleshooting

- **"thumb signals disagree"** — the rig's finger geometry is ambiguous; check
  the named bones, or re-run with `--dry-run` and inspect the map in
  `report/<sequence>.json`.
- **Ambiguous/wrong left-right pairing** on rigs without L/R bone names — set
  `swap_arms = true` in the config.
- **Hands sit too far forward/back on the grip** — tune `hand_center_fraction`
  (0 = wrist-anchored, 1 = fingertip-aligned) or set an explicit `hand_offset`.
- **Texture errors** — the gate lists the missing/invalid file and where it
  searched; textures must be 8-bit indexed BMPs.
- **`verify FAIL` with outputs on disk** — deliberate: files are kept for
  inspection, the non-zero exit code is the failure signal.
