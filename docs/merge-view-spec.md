# Technical specification: `merge-view` (draft for review)

**Status:** approved by the author (2026-07-31) — decisions folded in below;
implementation proceeds in the milestones of §7.
**Task:** merge a folder of decompiled view-models (e.g. `tmp/pistols/view`,
56 weapons) into combined GoldSource model(s), with every hand skeleton renamed
and re-hierarchised to `storage/hands/reference_hands.smd` conventions, the
`Finger{N}Nub` bones removed, each weapon **keeping its own hand meshes and
animations** (no hand replacement, no retargeting), and all weapons linked as
per-weapon submodels via `$bodygroup`.
**Prior art:** `MrCatOff/goldsource-models` (dev). Its `merge` pipeline is the
algorithmic base — battle-tested on the 58-model corpus. This spec ports its
proven semantics onto this repo's foundation (typed SMD/QC layer, text-level
verification gates, compile-proven writer, texture staging) rather than copying
code. Divergences from it are called out explicitly.

---

## 1. Objective

One command:

```bash
python -m valve_qc_merger merge-view tmp/pistols/view --out out/pistols [--name v_pistols]
```

turns N decompiled weapon directories into compile-ready output where:

1. every model's hand bones carry the reference names and hierarchy
   (`Bip01` → `Bip01 L Forearm` → `Bip01 L Hand` → `Bip01 L Finger0`,
   `Finger01`, `Finger02`, … `Finger4`, `Finger41`, `Finger42`, mirrored for R
   — **space-separated style, exactly as `reference_hands.smd` spells them**),
2. every `Finger{N}Nub` tip is deleted (10 bones per full rig), with exact
   transform folding so nothing moves,
3. each weapon's own hand meshes, weapon meshes, textures and animations
   survive unmodified except for bone renames/removals (which are applied
   uniformly to reference and animation SMDs, frame-exactly),
4. all weapons are linked into one merged model per output part:
   `$bodygroup "weapon"` with one entry per weapon, `$bodygroup "hands"`
   likewise, selected together through `pev_body` (recorded in `models.ini`).

### Non-goals

- No hand-mesh replacement and no grip retargeting (that is `retarget`'s job).
- No mesh decimation, no skin/texturegroup variants, no player-model handling
  (author decision). Texture downscaling and atlas packing ARE in scope —
  §3.12/§3.13.

---

## 2. Inputs

- A parent directory; every subdirectory containing exactly one `.qc` is a
  model (prior art `discover_models`). `--exclude NAME` drops models.
- The canonical skeleton: `storage/hands/reference_hands.smd` (45 bones; the
  10 Nubs occupy the trailing indices and are dropped, leaving the 35-bone
  canonical hand tree).
- Per-model manifest = its QC (this repo already parses bodygroups, sequences
  with SMD paths, attachments, hboxes, fps/events).

Corpus facts (pistols/view, 56 models): 40–110 bones each, 495 sequences
total, 54/56 rigs already carry hand-ish bone names, 2 are opaque `BoneNN`
rigs (v_elite family) — detection must therefore be geometric, not name-based.

---

## 3. Pipeline

Stages in order; each stage is pure-Python over parsed SMD/QC data (no
Blender — nothing here needs posing, only exact linear algebra which the prior
art has already validated to ~1e-9 over the corpus).

### 3.1 Discover & sanitise
Find models; ASCII-sanitise file names (Korean/Japanese decompile leftovers),
patching every reference inside SMD/QC text. Extensionless BMP textures get
`.bmp` appended (file + material lines) — this repo's texture gate already
implements the material side.

### 3.2 Load & normalise structure
Parse QC + all SMDs per model. Dedupe duplicate `$bodygroup` names (the merge
aligns groups by name; duplicates silently lose meshes otherwise — prior-art
lesson).

### 3.3 Hand rig detection (geometric)
Reuse the proven structural heuristics (prior art `hands.py`, which mirror the
machinery this repo already built for `retarget`):

- hand = bone with ≥4 finger chains of ≥2 joints (leaf stubs — reversed
  forearms, `Se_Hand` helpers, bullet bones — excluded, as in `retarget`);
- thumb = the chain whose removal leaves the other bases most colinear,
  cross-checked by abduction of **real joint positions** (never bone tails);
- finger order = along the best-fit knuckle line, oriented thumb-first;
- forearm chosen by matching the reference's forearm→hand distance (not
  blindly the parent — some rigs insert a stub wrist bone, some parent the
  forearm *below* the hand);
- L/R assignment scored per whole pairing with mirror penalty (signed-volume
  chirality); bone-name L/R tokens decide outright when both sides carry them.

Models whose rig cannot be matched confidently abort with a diagnostic listing
the model name (batch continues with `--skip-unmatched`, reported).

### 3.4 Canonical rename + re-hierarchy
Build `model bone → reference name` from the match; apply with a
collision-guarded rename map (a rename that would merge two distinct bones
aborts the model rather than corrupt it). Renames are applied to every SMD and
every QC bone reference (attachments, hboxes, controllers). Then enforce the
reference parentage for the renamed subtree (forearm→hand→finger chains) with
exact per-frame reparenting (`local = parent_world⁻¹ · world`) where a model's
hierarchy disagrees.

Rigs with fewer joints than the reference (2-joint fingers) keep their own
joint count: rename what matches (`Finger0`, `Finger01`), leave nothing
dangling. Extra non-finger children of the hand (helpers, bullet bones) keep
their names and simply remain parented under the renamed hand bone.

### 3.5 Nub removal + prune
Delete `Bip01 [LR] Finger{N}Nub` everywhere via the generic exact-fold bone
removal (fold `removed_local · child_local` into children per frame; Nubs are
leaves, so this is a pure deletion). If a Nub unexpectedly carries vertex
weight, rebind those vertices to its parent first and warn.
**Decision (Q3):** the full prune — all vertex-less, QC-unreferenced bones
are removed (this is what makes the bone budget work); `--no-prune` keeps
everything except the Nubs, which are always removed.

### 3.6 Bodygroup collapse
Per model, collapse switchable bodygroups to their first entry (one weapon
submodel per model — prior-art default and rationale: every kept group costs
one of 32 bodyparts and multiplies `pev_body`). `--keep-group MODEL:GROUP`
escape hatch preserved. Hand bodygroups are identified (name regex, else
>50 % of vertices on rig bones) and normalised to a single `hands` entry per
model — the model's **own** hand mesh.

### 3.7 Bone pooling (the 128-bone budget)
Shared set = the canonical 35 hand bones (identical across models after 3.4 —
this is the entire point of renaming onto one skeleton). Weapon-specific bones
are pooled into shared slots (`Bone_WPNJ{n}_TYPE1` convention) since only one
weapon draws at a time: largest model shapes the pool; slot assignment prefers
keeping a bone under its parent's slot (ancestor-claimed invariant so the
merged table has one parent per slot name); reparenting is per-frame exact.
Models missing the anchor bone get an inert copy. Budget: 127 (MAXSTUDIOBONES
− 1), predicted with studiomdl's real survival rule (vertex-referenced bones +
ancestors).

### 3.8 Merge
- Bone-table conflicts across models resolved by majority-vote parent; losers
  renamed `{model}__{bone}` with cascade (safety net — after 3.4/3.7 hand and
  pooled bones agree by construction).
- **Decision (Q2) — shared root = `Bip01`:** no injected `Universal_Root`;
  every model shares the canonical `Bip01` root after renaming (one slot
  saved). Weapons whose gun subtree hangs outside the hand tree get their
  root reparented under `Bip01`, per-frame exact.
- Sequences concatenated in model order, SMD paths prefixed `{model}/`;
  collisions `{model}__name`; `--index-sequences` renames to
  `{model}_seq_{i}` with originals preserved in `models.ini`; fps/events/loop
  carried verbatim.
- Textures deduped by md5; same-name-different-bytes renamed per model. The
  existing texture gate rules apply (ASCII, no spaces, 8-bit BMP, staged next
  to the QC).
- Attachments: GoldSource IDs 0–3, first model per ID wins. Hitboxes dropped
  by default (they reference pruned bones; `--keep-hitbox-bones` to keep).

### 3.9 QC + models.ini
Merged QC written fresh: `$bodygroup "weapon"` and `$bodygroup "hands"` with
one entry per model, aligned so index *i* selects weapon *i*'s mesh **and**
hands together via `pev_body` (mixed-radix encoding; blank-entry sharing so
absent groups do not explode the product). The per-model manifest
(`pev_body = N`, `anim_<name> = merged_index`) keeps the prior art's field
contract; format selectable via `--manifest-format ini|json|toml` (default
`ini` for drop-in compatibility; the same data structure serialised). Identical hand meshes across
models (the corpus reuses ~4 CSO hand meshes heavily) are collapsed to shared
entries by content hash, shrinking both the file and the vertex budget.

### 3.10 Output splitting
Hard limits force parts: ~80–90 textures (~30 weapons) per .mdl, 2048
verts/submodel, 64 KB animation data per sequence, ≤32 bodyparts, ≤127 bones
after pooling. The planner packs models into `<name>`, `<name>_part2`, … by
first-exceeded budget, deterministically (sorted by name), and `analyze` mode
(`--dry-run`) prints the plan + budgets without writing. For the 56-pistol
corpus expect ~2 parts (texture-bound).

### 3.11 Texture downscale (ported from prior art)

`--max-texture-size N`: any staged texture larger than N on either axis is
resampled down (aspect preserved, 8-bit palette re-quantised); UVs are
untouched because GoldSource UVs are normalised per texture. Applied before
atlas packing so the atlas inputs are already bounded.

### 3.12 Texture atlas packing (`--pack-textures`)

Author requirement: GoldSource caps textures per model (and studiomdl
degrades far earlier in practice), so textures can be packed four-to-a-file:

- Every participating texture is resampled to **256×256**; four of them are
  composited into one **512×512** BMP (2×2 grid). One atlas file replaces four
  texture slots.
- **Shared palette:** a GoldSource BMP carries one 256-colour palette, so the
  four members are re-quantised together (median-cut over the union of their
  pixels). This is the quality cost of packing; the report records per-atlas
  colour counts before/after.
- **UV rewrite:** every mesh vertex whose material moved into an atlas gets
  `u' = (clamp01(u) + col) / 2`, `v' = (clamp01(v) + row) / 2`, with a
  half-texel inset toward each tile centre so bilinear filtering cannot bleed
  a neighbour tile across the seam. UVs outside [0,1] (tiling) disqualify that
  texture from packing (detected per mesh; reported).
- **Compatibility grouping — never mixed in one atlas:**
  - `$texrendermode ... masked` textures (transparency lives at palette index
    255; masked pack only with masked, and index 255 is reserved in the shared
    palette),
  - `chrome` / `additive` render-mode textures are **excluded** from packing
    entirely (chrome UVs are generated by the engine, not the mesh),
  - textures referenced with tiling UVs (above).
- Packing groups are chosen per output part after model→part assignment, so a
  packed atlas never spans parts. Greedy fill, deterministic order; leftover
  groups of 2–3 still pack (remaining tiles duplicated black).
- `$texrendermode` lines are rewritten to the atlas names; `models.ini`
  records the original→atlas mapping.
- Escape hatches: `--no-pack-texture NAME` (glob) keeps a texture standalone;
  without `--pack-textures` nothing is packed.

### 3.13 Verification gate (this repo's addition)
Text-level, before declaring success — the prior art trusts its passes; here
every claim is re-proven from the emitted files:

| Check | Proves |
|---|---|
| canonical_hands | every output part's hand subtree = reference names+parents exactly; zero `*Nub` anywhere |
| pose_preserved | FK world transforms of every surviving bone, every frame of every sequence, equal the pre-merge originals within ε (renames/folds/reparents were exact) |
| geometry_preserved | every kept vertex position survives per mesh (weapon and hand meshes untouched) |
| tables_consistent | each part: one node table across its mesh + anim SMDs; parents-before-children ids |
| budgets | studiomdl-surviving bones ≤127, verts/submodel ≤2048, bodyparts ≤32, textures ≤ threshold, per-sequence RLE estimate ≤64 KB, paths ≤60 chars |
| pev_body | recomputed indices match `models.ini`; every model selectable |
| textures_valid | existing gate (ASCII, no spaces, 8-bit BMP, staged) |
| atlas_valid | when packing: every atlas is 512×512 8-bit, every rewritten UV lands inside its tile inset, masked textures kept index-255 transparency, chrome/additive textures untouched |

Exit codes follow `retarget`'s contract (0/2/3/4). Per-part `report.json`
records matches, renames, pool plan, budgets.

---

## 4. CLI sketch

```
merge-view <models-dir> --out DIR [--name v_pistols]
  [--reference SMD]              # default storage/hands/reference_hands.smd
  [--exclude NAME]... [--skip-unmatched]
  [--keep-group MODEL:GROUP]... [--keep-hitbox-bones]
  [--rename FIND=REPLACE]... [--index-sequences] [--max-sequences N]
  [--no-pool-bones] [--no-prune] [--dry-run]
  [--max-texture-size N] [--pack-textures] [--no-pack-texture GLOB]...
  [--manifest-format ini|json|toml]
  [--config TOML]
```

Config mirrors the flags plus per-model overrides; documented in
`docs/merge-view.md` + `configs/example_merge_view.toml` on implementation.

---

## 5. Testing

- Unit: rename-map collision guard; Nub fold exactness (FK before/after);
  reparent exactness; pool-slot ancestor invariant; pev_body encoding;
  splitting planner determinism; canonical-hands gate red/green.
- Corpus: full `tmp/pistols/view` run must pass the gate; `analyze` output
  reviewed for the 2 opaque rigs and any unmatched models.
- Compile check remains manual (studiomdl on the emitted QCs) as with
  `retarget`.

---

## 6. Author decisions (2026-07-31)

1. **Naming:** space-style reference names (`Bip01 L Hand`) — approved.
2. **Shared root:** `Bip01` (no `Universal_Root`).
3. **Prune:** full vertex-less prune, `--no-prune` escape (Nubs always go).
4. **Hands:** each weapon keeps its own hand meshes; identical meshes shared
   by content hash — approved. No male/female variant machinery here.
5. **Splitting:** automatic parts by budget — approved.
6. **Manifest:** field contract kept; format `ini` (default) | `json` | `toml`
   via `--manifest-format`.
7. **Scope:** decimation/skins/player-models out. Texture downscale IN
   (§3.11). Texture atlas packing IN (§3.12, `--pack-textures`).

## 7. Implementation milestones

- **M1** — package scaffold (`merge_view/`), discovery + sanitise + model
  loading on the typed QC/SMD layer; `--dry-run` inventory report. Unit tests.
- **M2** — hand rig detection + canonical rename + re-hierarchy + Nub removal
  + full prune, with the FK pose-preservation check as the development
  harness. Corpus target: all 56 pistols matched or explicitly diagnosed.
- **M3** — bodygroup collapse, bone pooling, merge (bones/sequences/
  attachments), QC + manifest generation, `Bip01` root unification.
- **M4** — texture stage: dedupe, downscale, atlas packing, UV rewrite.
- **M5** — splitting planner, full verification gate, docs
  (`docs/merge-view.md`) + `configs/example_merge_view.toml`, corpus run.

Each milestone lands with tests green and is exercised against
`tmp/pistols/view` before the next begins.
