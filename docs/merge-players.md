# `merge-players` — merge CSO player-character models into skin-bodygrouped CS 1.6 models

Unlike [`merge-p`](merge-p.md) (which merges `p_` *weapon* models), `merge-players`
merges third-person **player body** models: many Counter-Strike Nexon (CSO) skins
onto ONE canonical CS 1.6 rig. Each source body becomes an entry of a single
`skin` bodygroup, so a server can offer dozens of player skins from a handful of
`.mdl` files, all sharing the stock animation set.

```sh
python -m valve_qc_merger merge-players tmp/nexon_models \
    --base tmp/ORIGINAL_CS_MODEL --out out/players --name pl \
    --group-by size --pack-textures
```

## How it works

A **donor** model (`--base`, default `tmp/ORIGINAL_CS_MODEL` — arctic) supplies the
skeleton, the canonical ~111-sequence animation set, the hitboxes, attachments and
mouth controller. CSO player skeletons are *bind-compatible* with CS 1.6 — the core
`ValveBiped Bip01` joint rotations match the donor exactly — so no per-vertex
retarget is needed. For each CSO body the command:

1. **Collapses sub-bones.** CSO rigs add a few mesh-specific bones (`Breast_Sub`,
   `Eye_Sub`, `Spine_Sub`, `xmas_cap`, ...), each a child of a core Bip01 bone.
   Under the donor's shared animations they never move, so their vertices are
   rebound onto the nearest donor ancestor (exact — SMD vertex positions are
   absolute) and the empty bones dropped.
2. **Prunes vertexless leaves.** studiomdl unions reference bones by name and
   prunes globally-unused bones — and that pruning silently corrupts mesh strips.
   Pre-pruning leaves only the bones a body actually weights (plus their internal
   ancestors); the donor's animations still drive every survivor by name. Bones no
   body uses (fingers, mouth `Bone01`, twist helpers) simply never appear, and any
   `$hbox`/`$attachment`/`$controller` naming a pruned bone is dropped.
3. **Adopts the donor's sequences.** The donor's `$sequence` blocks are copied in
   order (blends, fps, loop, events intact) so the engine's index/activity lookup
   still resolves — the animations play correctly in game. CSO animations are
   discarded. Sequences the server does not need (`--placeholder-seq`, default the
   17 shield sequences) keep their slot but point at the tiny
   `I_am_a_stupid_placeholder` SMD — the same trick the donor itself uses — dropping
   the heavy blend data without shifting a single index.
4. **Rebases the animations to the group's proportions.** GoldSrc bakes bone
   *lengths* into a compiled animation, so a donor animation would impose arctic's
   proportions on every mesh — stretching skins whose skeleton differs (CSO female
   rigs have a half-length upper arm). Each group's shared animations are rebased
   so every non-root bone carries the group's own bind length (arctic's rotations
   are kept); the meshes keep their proportions and nothing stretches.
5. **Emits one bodygroup per body-part slot.** A high-poly CSO body ships split
   across several `$bodygroup` parts (to fit studiomdl's 2048-vertex/submodel
   cap); that split is preserved rather than concatenated. The merged model has
   `body0`, `body1`, … — one per part slot — each starting with `blank`. A skin
   sits at the same index (its position + 1) in every slot, `blank` where it has
   no part, so `pev_body` selects one skin's parts together. Each skin's
   `pev_body` value (mixed radix over the slots) is written to the manifest; a
   server sets it per player. Single-part bodies use `body0` only.

## Grouping (`--group-by`)

Models are partitioned into merge sets so that a shared animation set stays
plausible:

| Mode | Key |
| --- | --- |
| `size` (default) | skeleton proportion (quantised core-bone lengths) |
| `team` | `ct` / `t` label |
| `sex` | `female` / `male` label |

`size` groups by the actual skeleton proportions — the axis the stretch lives on.
Rigs with the same proportion signature share one (rebased) animation set with no
stretch, so the CSO female skeleton, the male one, tankers, monsters and chibi
models each form their own group automatically. Team and sex are **not** encoded in
the model data, so those modes classify by a built-in name-keyword table plus an
optional `--labels file.toml` override (`model = "ct"`); unlabeled models group as
`unknown` and are logged, never guessed.

Each group is split into parts (`p1/`, `p2/`, …) only when a compile budget forces
it: the per-bodypart submodel cap (`--submodel-limit`, default **32** for stock
studiomdl — raise it for a patched players compiler, e.g. `--submodel-limit 254`),
the texture budget, or `--max-skins`.

## Flags

- `--base DIR` — donor rig (default `tmp/ORIGINAL_CS_MODEL`).
- `--group-by {size,team,sex}`, `--height-tolerance F`, `--labels TOML`.
- `--placeholder-seq GLOB` (repeatable) — void matching sequence slots.
- `--include-base` — add the donor body as skin 0 (pulls in the full donor rig).
- `--max-skins N`, `--submodel-limit N` (default 32).
- `--exclude NAME`, `--manifest-format {ini,json,toml}`.
- `--texture-budget N`, `--max-texture-size N`, `--pack-textures`, `--no-pack-texture GLOB`.
- `--config TOML` ([`configs/example_merge_players.toml`](../configs/example_merge_players.toml)),
  `--no-verify`, `--dry-run`.

## Verification gate

Each emitted part is re-proven from its files: `skeleton_subset_of_donor` (every
bone is a donor bone), `no_vertexless_leaf` (nothing studiomdl would prune),
`geometry_preserved` (each skin's vertex positions+normals bit-match its source),
`sequences_canonical` (names+order == donor; voided slots point at the placeholder),
`budgets` (bones ≤ 127, submodels ≤ limit, textures ≤ 100). `--no-verify` skips it.

## Compiling

QC paths are emitted with forward slashes and SMDs with LF line endings, so a part
compiles as-is with the bundled macOS `tmp/studiomdl` — no backslash/CRLF fixups
(unlike the other merge commands). Each part directory is self-contained (QC +
`geometry/` + `anims/` + textures); compile inside it:

```sh
cd out/players/<group>/ && studiomdl pl_<group>.qc
```

The stock 32-submodel cap holds unless you compile with a raised-limit
players-specific studiomdl (then set `--submodel-limit` to match, e.g. 254).
