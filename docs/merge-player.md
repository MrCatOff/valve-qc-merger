# `merge-player` — merge decompiled p_ weapon models into combined GoldSource models

Takes a folder of decompiled p_ (player-held) weapon models and merges them
into as few compilable `.mdl` files as studiomdl's hard limits allow. The
engine poses an attached p_ model by bone-NAME merge against the player model,
so a merged part carries the shared `Bip01` arm chain once, **one** uniquely
named bone per held object (two for dual-wield, one per hand), one geometry
submodel per weapon and a single-frame `idle` sequence. One `pev_body` value
selects a weapon; `pev_body 0` shows nothing (the bodygroup leads with
`blank`). Every part is re-verified from the emitted files before the run
reports success.

## Quick start

```bash
python -m valve_qc_merger merge-player tmp/player_pistols \
    --out tmp/merged_p_pistols --name p_pistols
```

## What ends up in `--out`

```
out/
  models.ini            one section per weapon: which part .mdl, pev_body,
                        skins = N and skin_<i> = <texture> rows for skin variants
  inventory.json        per-model analysis record (weapon bones, collapses)
  p1/ ... pN/           one directory per compiled part:
    p_<name>_pN.qc      compile-ready QC (weapons bodygroup, $texrendermode,
                        merged $texturegroup, one idle sequence)
    geometry/<model>.smd   one submodel per weapon, own bind kept
    animations/idle.smd    single frame; every weapon bone at its own
                           hand-relative in-game pose
    *.bmp               staged textures (sanitised names) + skin variants
```

Compile each part from inside its directory (`studiomdl p_<name>_pN.qc`; on
macOS convert QC backslashes first — `tools/build_studiomdl.sh` builds a
native compiler with the `$texrendermode` extension).

## How the skeleton is reduced

- Bones named `Bip01*` are **shared**: the engine overwrites them from the
  player model at runtime (bone-merge by name), so the corpus-wide variation
  in arm poses is irrelevant — only the hand-relative offset of a weapon bone
  decides where the weapon sits.
- Every non-shared subtree (typically `flash -> weapon`) is collapsed to its
  single vertex-bearing bone: vertices rebound (positions are model-space, so
  nothing moves), the survivor reparented under its anchoring hand with a
  per-frame-exact re-solve, helpers folded away.
- The survivor is renamed to the model's directory name (`p_anaconda`), which
  is unique across the corpus — decompiled bone names collide freely
  (`colt_p`, `Object02`, three different `Luger_P_08_Low`s). In a dual-wield
  pair the right-hand bone owns the base name; the left one gets `_L`.
- Models whose meshes ride the shared arm bones directly (two-handed knives
  like `p_balrog9`) cost zero extra bones.

Geometry SMDs keep their **own** skeleton bind values (studiomdl recomputes
per-file bonefixup), so vertex localisation is byte-faithful; the merged
`idle` frame carries each weapon bone's local transform from its own model's
idle animation.

## Skins, render modes, attachments

- `$texturegroup` skin rows (CSO +6/+8 upgrade skins, mask variants) are
  merged column-wise into one `skinfamilies` block; models with fewer rows
  repeat their last row. Only one weapon is visible at a time, so a global
  skin row is safe. `models.ini` records `skins = N` plus one `skin_<i> = <texture>` line per row for those weapons, so a plugin can identify each variant and select it with `pev_skin = i`.
- `$texrendermode` entries are carried per staged texture, as in merge-view.
- Per-weapon `$attachment` entries are **dropped** (with a warning): GoldSrc
  caps a model at 4 attachments, so 30 per-weapon muzzle-flash points cannot
  survive a merge. The classic community weapons.mdl ships the same way.
- Empty decompiler submodels (0-triangle `upgrade.smd` placeholders) are
  dropped with a warning.

## The verification gate

| Check | Proves |
|---|---|
| `tables_consistent` | one node table across the part's SMDs, parents before children |
| `placement_preserved` | shared bones keep their bind locals verbatim; every weapon bone's hand-relative transform (geometry bind AND idle frame) matches the pristine original |
| `geometry_preserved` | every merged mesh vertex bit-matches an original vertex |
| `budgets` | bones ≤ 127, submodels ≤ 32, verts/normals ≤ 2048 per submodel, textures ≤ 100 and present on disk, labels < 32 chars, QC paths ≤ 60 chars |

Any failed check fails the run (exit 2). `--no-verify` skips the gate.

## Flags

| Flag | Meaning |
|---|---|
| `models_dir` | parent directory; each subdir with exactly one `.qc` is a model |
| `--out DIR` (required) | output directory |
| `--name STEM` | output model name stem (default `p_merged`) |
| `--exclude NAME` | skip a model directory (repeatable) |
| `--manifest-format ini\|json\|toml` | manifest format (default ini) |
| `--texture-budget N` | max textures per part (default 80; hard engine cap 100) |
| `--max-texture-size N` | downscale staged textures larger than N on either axis |
| `--pack-textures` | pack eligible textures into 512×512 atlases (skin-family textures stay standalone) |
| `--no-pack-texture GLOB` | keep matching textures out of atlases (repeatable) |
| `--config TOML` | supply defaults for any flag (explicit CLI values win) |
| `--no-verify` | skip the verification gate |
| `--dry-run` | discover, sanitise and analyse only; print the inventory |

## Hard limits this exists to respect

Same unchecked studiomdl arrays as merge-view (see `docs/merge-view.md`): a
part holds at most 32 submodels (the leading `blank` + 31 weapons), 127 bones
and 100 textures. With one bone per weapon a part exhausts submodels long
before bones — 31 single-hand pistols cost 11 shared + 31 weapon bones.
