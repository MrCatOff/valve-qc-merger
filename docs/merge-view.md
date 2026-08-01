# `merge-view` — merge decompiled view-models into combined GoldSource models

Takes a folder of decompiled view-models (one weapon per subdirectory) and
merges them into as few compilable `.mdl` files as studiomdl's hard limits
allow. Hand bones are renamed and re-hierarchised onto the reference skeleton
(`storage/hands/reference_hands.smd`), weapon bones share a pooled slot table,
each weapon keeps its own hand meshes, and one `pev_body` value selects a
weapon's meshes together. Every part is re-verified from the emitted files
before the run reports success.

## Quick start

```bash
python -m valve_qc_merger merge-view tmp/pistols/view \
    --out tmp/merged_pistols --name v_pistols

# With texture packing and rewritten sound paths:
python -m valve_qc_merger merge-view tmp/pistols/view \
    --out out --name v_pistols \
    --pack-textures --max-texture-size 512 \
    --sound-path 'csforce/pistols/${fileBasename}'

# Everything from a config file:
python -m valve_qc_merger merge-view tmp/pistols/view --out out \
    --config configs/example_merge_view.toml
```

## What ends up in `--out`

```
out/
  models.ini            one section per weapon: which part .mdl, pev_body,
                        anim_<name> = sequence index; [textures_pN] sections
                        map packed originals to atlas tiles
  inventory.json        per-model load/canonicalisation record
  canonical/<model>/    the canonicalised (pre-merge) model, for inspection
  p1/ ... pN/           one directory per compiled part:
    v_<name>_pN.qc      compile-ready QC ($bodygroup, $texrendermode,
                        $sequence with fps + events)
    v_<model>/          per-weapon meshes (weapon.smd, weapon_2.smd, hands.smd)
                        and that weapon's animation SMDs
    *.bmp               staged textures (sanitised names) and atlases
```

Compile each part from inside its directory (`studiomdl v_<name>_pN.qc`;
on macOS convert QC backslashes first — see `tools/build_studiomdl.sh` for a
native compiler with the `$texrendermode` extension).

## How a weapon is selected at runtime

Bodygroups are aligned by model position: the `weapon` group has one entry
per weapon, the `hands` group has that weapon's own hands at the same index
(`blank` where a model has none), and `weapon_2`/`weapon_3` carry extra
always-on submodel groups. `models.ini` gives the ready-made `pev_body` value
per weapon and the merged sequence index for every original animation name —
identical animations are deduped within a part, so recolour variants share
sequence indices.

## The pipeline

1. **Discovery + sanitise** — one `.qc` per subdirectory; byte-level encoding
   fixes are applied in place and recorded in `inventory.json`.
2. **Hand canonicalisation** — geometric rig detection maps each model's hand
   bones onto the reference names/hierarchy (FK-exact renames + reparents);
   only `Finger*Nub` bones are removed (`--prune` opts into the full prune).
3. **Bodygroup collapse** — always-on weapon groups concatenate into one
   submodel; the hand group is identified by name or vertex-weight share.
4. **Part split** — greedy packing under hard budgets: 32 submodels
   (studiomdl silently corrupts memory past its fixed arrays), 127 bones
   under pooled slots, texture and sequence budgets.
5. **Bone pooling** — weapon bones share `Bone_WPNJ{n}_TYPE1` slots
   (largest model shapes the pool, structure-matched reuse, reshaping only
   once the pool is full). A byte-exact studiomdl size replica previews every
   plan; one that would push a sequence past the 64K anim-stream cap falls
   back to reparent-free pooling.
6. **Merge** — one identical node table stamped into every SMD (missing bones
   grafted static, canonical parents enforced with per-frame re-solves);
   meshes keep their own binds and untouched vertices; sequences deduped by
   (content, fps, events); textures staged with sanitised names,
   `$texrendermode` carried, optional downscale/atlas packing.
7. **Verification gate** — re-proven from the emitted files, per part:

   | Check | Proves |
   |---|---|
   | `tables_consistent` | one node table across the part's SMDs, parents before children |
   | `canonical_hands` | reference hand subtree embedded exactly; zero `*Nub` bones |
   | `pose_preserved` | FK worlds of every mapped bone vs the pristine originals (radius-aware tolerance: 6-decimal SMD text legitimately moves a bone parked 10 000 units away by millimetres) |
   | `geometry_preserved` | every merged mesh vertex bit-matches an original vertex |
   | `budgets` | bones ≤ 127, submodels ≤ 32, bodyparts ≤ 32, verts/normals ≤ 2048 per submodel, textures ≤ 100, exact per-sequence anim stream ≤ 64K, QC paths ≤ 60 chars |

   Any failed check fails the run (exit 2). `--no-verify` skips the gate.

## Flags

| Flag | Meaning |
|---|---|
| `models_dir` | parent directory; each subdir with exactly one `.qc` is a model |
| `--out DIR` (required) | output directory |
| `--name STEM` | output model name stem (default `v_merged`) |
| `--exclude NAME` | skip a model directory (repeatable) |
| `--reference SMD` | canonical hand skeleton (default `storage/hands/reference_hands.smd`) |
| `--skip-unmatched` | continue past models whose rig cannot be matched |
| `--prune` | also fold away vertex-less unreferenced bones (default keeps everything except `Finger*Nub`) |
| `--no-pool-bones` | skip bone pooling (merged table may exceed 127) |
| `--manifest-format ini\|json\|toml` | manifest format (default ini) |
| `--texture-budget N` | max textures per part (default 80; hard engine cap 100) |
| `--sequence-budget N` | max sequences per part after dedupe (default 111) |
| `--max-texture-size N` | downscale staged textures larger than N on either axis |
| `--pack-textures` | pack eligible textures four-to-a-file into 512×512 atlases |
| `--no-pack-texture GLOB` | keep matching textures out of atlases (repeatable) |
| `--sound-path TEMPLATE` | rewrite sound event paths for every weapon; `${fileBasename}` is the original file name (e.g. `csforce/pistols/${fileBasename}`) |
| `--config TOML` | supply defaults for any flag (explicit CLI values win) |
| `--no-verify` | skip the verification gate |
| `--dry-run` | discover, sanitise and load only; print the inventory |

## Texture packing rules

Eligible textures are resampled to 256×256 and composited four-to-a-file into
a 512×512 8-bit BMP with one shared median-cut palette; referencing UVs are
remapped into the tile with a half-texel inset. Never mixed into one atlas:
`masked` textures pack only with masked ones (transparency index 255 stays
reserved), `chrome`/`additive`/`fullbright` textures never pack, textures
referenced with tiling UVs never pack, and `--no-pack-texture` globs stay
standalone. Groups of 2–3 leftovers still pack; a lone leftover stays
standalone. `models.ini` records `original -> atlas.bmp:tile`.

## Hard limits this exists to respect

studiomdl (HLSDK and community forks) has unchecked fixed arrays: more than
32 submodels silently corrupts memory (meshes detach from bones), sequence
labels of 32+ characters overflow a `strcpy`, spaces in texture names crash
the SMD parser, one sequence's animation stream cannot exceed 64K (u16
offsets), and only vertex-carrying bones (plus ancestors) survive into the
compiled bone table. The merge is shaped around all of these; the gate and
`tools/studiomdl` prove each part actually compiles.
