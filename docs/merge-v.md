# `merge-v` — merge decompiled view-models into combined GoldSource models

Takes a folder of decompiled view-models (one weapon per subdirectory) and
merges them into as few compilable `.mdl` files as studiomdl's hard limits
allow. Hand bones are renamed and re-hierarchised onto the reference skeleton
(`storage/hands/reference_hands.smd`), weapon bones share a pooled slot table,
and one `pev_body` value selects a weapon. By default each weapon keeps its own
hand meshes (aligned to the weapon); with `--shared-hands` — for inputs that
already wear our hands, e.g. the `retarget` output — all weapons share ONE
male/female hands bodygroup, keeping `pev_body` under the 255 `WRITE_BYTE` cap.
Every part is re-verified from the emitted files before the run reports success.

## Quick start

```bash
python -m valve_qc_merger merge-v tmp/pistols/view \
    --out tmp/merged_pistols --name v_pistols

# With texture packing and rewritten sound paths:
python -m valve_qc_merger merge-v tmp/pistols/view \
    --out out --name v_pistols \
    --pack-textures --max-texture-size 512 \
    --sound-path 'csforce/pistols/${fileBasename}'

# Everything from a config file:
python -m valve_qc_merger merge-v tmp/pistols/view --out out \
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
    v_<model>/          per-weapon meshes (weapon.smd, weapon_2.smd, hands.smd
                        unless --shared-hands) and that weapon's animation SMDs
    hands/              (--shared-hands only) the one shared hands_female.smd +
                        hands_male.smd used by every weapon
    *.bmp               staged textures (sanitised names) and atlases
  standalone/<model>/   a model the merge could not take (multi-part under
                        --shared-hands, other hands, an unmatched rig, a
                        pose that would not survive, a header it rejects),
                        copied as it is with $modelname "<model>.mdl" — in
                        models.ini with model = <model>.mdl, pev_body = 0,
                        standalone = 1, reason, hands = ours | own (its own
                        hands: retarget failed or never ran), hand_stride
                        when it has male/female hands, and its own anim_*
```

Nothing is dropped by default: every weapon given to the build is shipped,
merged or on its own (each standalone model takes a model slot). Models that
are only hands (zombie claws) are still rejected — they are not weapons.
`--no-standalone` restores the old behaviour (rejected models left out).

Compile each part from inside its directory (`studiomdl v_<name>_pN.qc`). QC
studio paths use forward slashes, so the native macOS compiler (see
`tools/build_studiomdl.sh` for one with the `$texrendermode` extension)
resolves them directly.

## How a weapon is selected at runtime

**Default (per-weapon hands).** Bodygroups are aligned by model position: the
`weapon` group has one entry per weapon, the `hands` group has that weapon's own
hands at the same index (`blank` where a model has none), and
`weapon_2`/`weapon_3` carry extra always-on submodel groups. One `pev_body`
value pairs a weapon with its hands, so the byte scales as `weapon × hands` and
nears 255 by ~16 weapons.

**`--shared-hands`.** The `hands` group is emitted FIRST as an independent
2-entry dimension (`hands_female`, `hands_male`) shared by every weapon, and the
`weapon` group second. `pev_body = weapon_index × 2 + hand` — set the weapon by
its `pev_body` from `models.ini`, then OR the low bit for the male hand. The
byte is therefore `2N − 1` (well under 255; 42 pistols → max 47) instead of
`weapon × hands`. This is valid only when the inputs share the same hand bind
(the grip lives in the sequences, not the mesh) — the merge checks it: hand
meshes are compared in bone-local space (each vertex in its bone's bind frame,
so differently seated retargets still agree), the models wearing the most
common hands are kept and every other one is rejected with a "wears other
hands (not retargeted?)" failure; the `shared_hands` gate row reports the
count. A **multi-part weapon** (more
than one always-on weapon submodel) is rejected — each extra weapon bodygroup
would multiply the byte, so ship such a weapon on its own.

**Attachments (muzzle flash, shell eject).** GoldSrc keeps 4 attachments per
*model*, each a fixed offset from one bone, so per-weapon `$attachment` lines
cannot be copied over. The merge instead emits shared slot bones
`attachment0..3` (only up to the highest index any weapon uses) and
`$attachment N "attachmentN" 0 0 0`. Indices keep their source meaning (game
code reads them: deagle 0 = flash / 1 = shell; elite 0/1 = left/right flash,
2/3 = left/right shell). Every sequence of every weapon animates slot N to
that weapon's own attachment N (its bone's pose composed with the QC offset),
so muzzle events and shell ejects fire from the active weapon's points.

- Each slot hangs off the **wrist** (`Hand.L`/`Hand.R`, or the ValveBiped
  hands) its points stay closest to across all weapons — relative to the
  gripping hand a muzzle barely moves, so the channels are near-constant
  (small quantisation steps, RLE-cheap streams; ~50-80 KB smaller parts).
  `Bip01` when no wrist bone exists.
- A weapon that parks a slot off-map (CSO hides unused shell ejects at
  `-1000000`) gets that slot a carrier `attachmentN_base`: the carrier holds
  near poses, the leaf only far ones. studiomdl quantises each bone channel
  with ONE scale over every sequence, so a shared channel would coarsen every
  other weapon's muzzle to ~30u steps.
- A weapon without slot N parks it at the slot's parent; indices ≥ 4 are
  dropped with a warning.
- Slot bones are reserved in the 127-bone budget before pooling, and each
  carries one zero-area anchor triangle (an existing weapon vertex, tripled):
  studiomdl drops vertex-less bones, and an attachment on a dropped bone
  fails to compile.

With `--shared-hands` the reference defaults to the CSO hands
(`storage/handswap/cso_reference_hands.smd`) instead of the ValveBiped
`reference_hands.smd`. The retarget output wears the CSO hands, whose arm is a
full four-bone chain (`UpperArm -> Arm0 -> Arm1 -> Hand`); the ValveBiped
reference has only a two-bone arm (`Forearm -> Hand`), so matching against it
drops `UpperArm`/`Arm0` and rebinds the upper-arm mesh onto the forearm — the
elbow then deforms as the arm animates. The CSO reference keeps the full chain
(`Bip01 -> UpperArm -> Arm0 -> Arm1 -> Hand`), so the upper arm keeps its own
bones. Pass `--reference` to override.

`models.ini` gives the ready-made `pev_body` value per weapon and the merged
sequence index for every original animation name — identical animations are
deduped within a part, so recolour variants share sequence indices.

## Skins and header commands

**Skins.** A view model's `$texturegroup` (CSO upgrade skins: `Luger_v_6`,
`Luger_v_8`, …) cannot stay a skin family in a merged model: the server sets a
view model's `body` (sent with the weapon animation), never its `skin`. Each
extra skin row therefore becomes its **own weapon entry** `<model>_skin<k>`
(k = the row number): the weapon meshes retextured with that row, the same
hands, bones and animations. Identical sequences are deduped within a part,
so a variant costs one weapon submodel (one `pev_body` value) and its
textures — no animation data. A row that changes none of the weapon's
textures adds nothing. `models.ini` lists the variants like any weapon
(same `anim_*` indices as the base). `--no-skin-variants` keeps only the
first row (the pre-skins behaviour).

**Header commands.** The merged QC is written with `$scale 1.0`, no
`$origin` and `$flags 0`. An input whose QC relies on anything else would
silently change once merged, so `$scale ≠ 1` and a non-zero `$origin` reject
the model (merge it on its own, or bake the transform into its SMDs) and a
non-zero `$flags` is dropped with a warning (one model-wide value).

**Sequence options.** Merged `$sequence` blocks carry the animation, events,
`fps`, `loop` and the `ACT_*` activity with its weight (the engine plays view
model sequences by index, but the information stays). A **blend** (several
animation SMDs in one block) would keep only its first animation, so such a
model is rejected; other options merge-v does not rebuild (`origin`,
`rotate`, motion extraction `LX`/`LY`…, frame ranges, `node`/`transition`)
are reported as "not carried" warnings.

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
| `--reference SMD` | canonical hand skeleton (default `storage/hands/reference_hands.smd`; with `--shared-hands`, `storage/handswap/cso_reference_hands.smd` — the CSO hands with the full arm, so the elbow is preserved) |
| `--skip-unmatched` | continue past models whose rig cannot be matched |
| `--shared-hands` | inputs already wear our male/female hands (e.g. the `retarget` output): emit ONE shared hands bodygroup (`pev_body = weapon × 2 + hand`) instead of per-weapon hands; a multi-part weapon is first folded into one submodel (see `--max-decimation`), and rejected only when that fails |
| `--no-skin-variants` | keep only each weapon's first `$texturegroup` row instead of one weapon entry `<model>_skin<k>` per extra row (see *Skins and header commands*) |
| `--max-decimation F` | with `--shared-hands`: a weapon whose always-on parts exceed one 2048-vertex submodel is folded into ONE submodel when removing at most this fraction of its vertices fits it (default 0.15; 0 disables). Half-edge collapses only — every kept vertex keeps its exact position and bone; UV seams, material borders and open edges are never touched, so the silhouette and texturing stay intact. The log reports `folded N parts … -p%, surface error <= e u` |
| `--prune` | also fold away vertex-less unreferenced bones (default keeps everything except `Finger*Nub`) |
| `--no-pool-bones` | skip bone pooling (merged table may exceed 127) |
| `--manifest-format ini\|json\|toml` | manifest format (default ini) |
| `--texture-budget N` | max textures per part (default 80; hard engine cap 100) |
| `--sequence-budget N` | max sequences per part after dedupe (default 111; 255 with `--shared-hands`, where 31 weapons fit the submodel cap — the game selects a viewmodel animation by a byte) |
| `--max-texture-size N` | downscale staged textures larger than N on either axis |
| `--pack-textures` | pack eligible textures four-to-a-file into 512×512 atlases |
| `--no-pack-texture GLOB` | keep matching textures out of atlases (repeatable) |
| `--sound-path TEMPLATE` | rewrite sound event paths for every weapon; `${fileBasename}` is the original file name (e.g. `csforce/pistols/${fileBasename}`) |
| `--config TOML` | supply defaults for any flag (explicit CLI values win) |
| `--no-standalone` | leave out the models the merge cannot take instead of shipping each on its own (`standalone/<model>/`) |
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
