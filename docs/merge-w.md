# `merge-w` — merge decompiled w_ weapon models into combined GoldSource models

Takes a folder of decompiled w_ (dropped-weapon) models and merges them into
as few compilable `.mdl` files as studiomdl's hard limits allow. A w_ model
renders at its entity origin with a static idle pose — no player skeleton is
involved — so the merged part carries just **two bones**: a `flash` root and
a `weapon` bone holding every vertex. With every weapon on one bone,
studiomdl auto-generates exactly **one hitbox** that always covers the
visible weapon (per-weapon bones would leave the hitboxes of invisible
weapons active — `$hbox` traces would hit them). One `pev_body` value selects
a weapon; `pev_body 0` shows nothing.

## Quick start

```bash
python -m valve_qc_merger merge-w tmp/pistols_world \
    --out tmp/merged_w_pistols --name w_pistols
```

## What ends up in `--out`

```
out/
  models.ini            one section per weapon: which part .mdl, pev_body,
                        skins = N and skin_<i> = <texture> rows for skin variants
  inventory.json        per-model record (bake distance, warnings)
  p1/ ... pN/           one directory per compiled part:
    w_<name>_pN.qc      compile-ready QC (weapons bodygroup, $texrendermode,
                        merged $texturegroup, one idle sequence, NO $hbox —
                        studiomdl generates the single box itself)
    geometry/<model>.smd   one submodel per weapon, all verts on 'weapon'
    animations/idle.smd    single identity frame
    *.bmp               staged textures (sanitised names) + skin variants
```

## Why baking makes this exact

A w_ model draws every vertex at `idle_world · bind_world⁻¹ · v`. Those two
poses usually coincide, but not always — the infinity series' idle sits 9+
units from its bind. `merge-w` bakes that per-bone rigid transform into
the vertices first (identity for most models, so their vertices stay
bit-identical), after which all meshes genuinely live in their on-screen
space and can share one identity bone with zero loss. The gate re-derives
the on-screen positions from the pristine decompiles and requires an exact
match with what was emitted.

Multi-bone sources (two-root dual models, decompiler chains) collapse the
same way: after baking, bone assignment no longer matters, so every vertex
is rebound to `weapon`.

## Skins, render modes, hitboxes

- `$texturegroup` skin rows merge column-wise into one `skinfamilies` block
  (only one weapon renders at a time); `models.ini` records `skins = N` plus one `skin_<i> = <texture>` line per row, so a plugin can identify each variant and select it with `pev_skin = i`.
- `$texrendermode` entries are carried per staged texture.
- Original per-model `$hbox` lines are **not** carried: their bones no longer
  exist. The auto-generated box on `weapon` is the union of the part's
  weapons — always covering the visible one.
- Empty decompiler submodels are dropped with a warning.

## The verification gate

| Check | Proves |
|---|---|
| `tables_consistent` | every part SMD carries the identical identity `flash -> weapon` table |
| `render_preserved` | every merged vertex sits exactly where the original model rendered it (bit-level after writer quantisation) |
| `budgets` | submodels ≤ 32, verts/normals ≤ 2048 per submodel, textures ≤ 100 and present on disk (skin rows included), QC paths ≤ 60 chars |

Any failed check fails the run (exit 2). `--no-verify` skips the gate.

## Flags

Same surface as `merge-p` (see `docs/merge-p.md`): `--out`,
`--name` (default `w_merged`), `--exclude`, `--manifest-format`,
`--texture-budget`, `--max-texture-size`, `--pack-textures`,
`--no-pack-texture`, `--config`, `--no-verify`, `--dry-run`.
`configs/example_merge_world.toml` lists every config key.
