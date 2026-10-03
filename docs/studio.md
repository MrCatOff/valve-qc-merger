# Studio (GUI) — design and progress

A desktop IDE for building model packs: create a project, import models
(v, p, w, player bodies, zombie hands), retarget, merge, manage bones and
compile — on Windows first. The GUI is PySide6 with an own OpenGL viewport
(no VTK); `.mdl` files are imported in-process (no external decompiler,
since the exe cannot ship one).

| Milestone | Scope | Status |
| --- | --- | --- |
| M0 | service layer + project format | done |
| M1 | pure-Python `.mdl` importer | done |
| M2 | GUI shell: projects, import, explorer, inspector, log | done |
| M3 | OpenGL viewport: textures, sequences, bodygroups, bone/attachment overlay | done |
| M4 | builds in the GUI: options forms, budgets, gates, compile, manifest | done |
| M5 | bone tools: rename / reparent / delete, attachments | done |
| M6 | packaging: windowed PyInstaller exe + CI | done |

## Services (M0)

Every operation is `run_<op>(options, reporter) -> ServiceResult` in
`valve_qc_merger.services`:

| Service | Options | CLI |
| --- | --- | --- |
| `run_retarget` | `RetargetOptions` | `retarget` |
| `run_merge_view` | `MergeViewOptions` | `merge-v` |
| `run_merge_player` | `MergePlayerOptions` | `merge-p` |
| `run_merge_world` | `MergeWorldOptions` | `merge-w` |
| `run_merge_players` | `MergePlayersOptions` | `merge-players` |
| `run_merge_zhands` | `MergeZhandsOptions` | `merge-zhands` |
| `run_compile` | `CompileOptions` | — |
| `run_decompile` | `DecompileOptions` | `decompile` |

- Options are dataclasses with the CLI flag names (`options_from(cls,
  argparse_namespace)`; `options_to_dict` / `options_from_dict` for TOML).
- A `Reporter` receives log lines (the default prints — the CLI output is the
  service's log, byte-identical to before), `progress(done, total, label)`
  per model and per part, and is polled between units: when `cancelled()`
  is true the service raises `Cancelled`. `CallbackReporter` takes callbacks
  and a `threading.Event`; `CollectingReporter` keeps lines in memory.
- `ServiceResult` carries `exit_code` (0 ok, 2 fail, 3 bad inputs),
  `outputs` (emitted QCs; for compile, the .mdl), `failures`, `warnings`,
  `gates` (part, check, passed, detail) and the `manifest`.
- `run_compile` normalises the QC folder (LF endings, `/` in the QC), deletes
  a stale `.mdl`, streams studiomdl's output and succeeds only when studiomdl
  exits 0 AND wrote the `.mdl` (some builds exit 0 after an error).

## Project format

```
MyPack/
  project.toml
  assets/<kind>/<name>/   decompiled model (QC + SMD + BMP), kind = v|p|w|player|zhands
  builds/<build>/
    input/                staged copies of the build's assets (services sanitise in place)
    retarget/<asset>/     merge-v builds with retarget = true
    output/               the merge output (QC + SMDs + textures + manifest, .mdl after compile)
    last_run.json         exit code, outputs, failures, gate rows, duration
```

```toml
[project]
name = "CSO Pack"
format = 2                # 2 added categories; format-1 files open unchanged
categories = ["pistols", "submachines"]   # kept even while empty

[settings]
studiomdl = "C:/tools/studiomdl.exe"
game_dir = "C:/Games/Half-Life/cstrike"   # Deploy target
deploy_after_compile = false

[[assets]]
name = "v_deagle"
kind = "v"
path = "assets/v/v_deagle"
source = "D:/dump/v_deagle"
notes = ""
category = "pistols"      # absent = uncategorized

[[builds]]
name = "pistols"
kind = "merge-v"          # merge-v | merge-p | merge-w | merge-players | merge-zhands
assets = []               # empty: every asset of the kinds this build accepts
category = "pistols"      # with assets = []: only that category's (absent = all)
retarget = true           # merge-v only: retarget each asset, then merge with shared_hands

[builds.options]          # the service options (models_dir/out are set by the build)
name = "v_sh"
skip_unmatched = true
```

**Kinds on import** come from the name prefix (`v_`, `p_`, `w_`, otherwise
`player`); a `v_<zombie>_grenade` holding the frog grenade (texture
`frogbomb*`) turns that zombie's `v_<zombie>_knife[_variant]` and grenade
models into `zhands`. `Project.set_kind` overrides (and moves the folder).

**API** (`valve_qc_merger.project`): `Project.create(root, name)`,
`Project.open(root)`, `save()`, `import_decompiled(path, kind=None)` (one
model folder or a folder of them), `remove_asset`, `set_kind`,
`add_build(Build(...))` (validates the kind and every option name),
`remove_build`, `run_build(name, reporter)`, `compile_build(name, reporter)`.

## Categories

Assets can be filed under ONE category each (`pistols`, `submachines`, …);
the Explorer then shows `Assets ▸ <category> ▸ View models / Player-held /
World ▸ assets`, with `Uncategorized` last (no categories: the flat kind
groups as before). A category is project metadata only — the files stay in
`assets/<kind>/<name>`, so renaming or moving is instant.

- **Weapon link**: `v_deagle`, `p_deagle`, `w_deagle` (name without the
  `v_`/`p_`/`w_` prefix) and assets derived from them are one weapon. Moving
  one offers to move the others; an import without a category joins the
  category its weapon already has (`w_deagle` follows `v_deagle`); a derived
  asset takes its source's category.
- **Filing**: the import dialog asks for a category (automatic / pick /
  type a new one); Explorer ▸ right-click ▸ **Move to category** (several
  selected at once), or drag assets onto a category, a kind group or an
  asset inside it. Right-click a category: New / Rename / Delete (its assets
  become uncategorized) / **Create builds for this category**.
- **Category builds**: a build with no picked assets can take every asset of
  its kinds in one category (Settings ▸ Assets), so new pistols join the
  pistols build by themselves. *Create builds for this category* makes one
  build per kind present — `<category>_v` (merge-v, "on our hands first",
  output `v_<category>`), `<category>_p`, `<category>_w`, … — skipping
  names that exist.

## `.mdl` import (M1)

`valve_qc_merger.mdl` reads GoldSource v10 models in pure Python
(`read_mdl`) and writes the same QC + SMD + BMP folder layout as
tools/decompmdl (`decompile_mdl`); `valve-qc-merger decompile <file|folder>
--out DIR` and `Project.import_mdl` use it, so the Windows exe needs no
external decompiler.

- Covered: bones, controllers, hitboxes, attachments, sequences (fps, loop,
  activity, events, blends, motion flags, transitions; RLE animation
  decoding, `LX/LY/LZ` linear motion added back), sequence-group files
  (`<name>01.mdl`), textures (8-bit + palette, render-mode flags) including
  `$externaltextures` (`<name>T.mdl`), skin families (`$texturegroup`),
  bodyparts with blank submodels, strips and fans.
- Conventions proven by round trip: animation ROOT bones are turned back by
  -90 deg about Z (studiomdl turns them +90 when compiling) while reference
  SMDs keep the stored bind; triangles are written in the reverse of the
  drawn order (studiomdl reverses SMD winding when it builds strips).
- `$cliptotextures` is emitted (stock studiomdl crops textures to their UV
  bounds otherwise: 512 -> 500x501). Texture names drop non-ASCII bytes,
  turn spaces into `_` and keep a 40-character stem: studiomdl crashed
  (SIGTRAP, no output) on a 63-character texture name. Merges cap staged
  texture names at 56 characters for the same reason.
- Validated on 78 models (56 CSO pistols, 13 zombie hands, zp-cso grenades,
  knife and a player model, our compiled packs): against tools/decompmdl
  every triangle and every animation frame matches (where decompmdl works —
  it silently dies on the player model, which we decompile with all 111
  sequences); decompile -> stock studiomdl -> re-read keeps every model
  compilable, all 488,630 triangles present (29 of 223,559 non-sliver
  triangles change winding, studiomdl's own stripping), sequences, events,
  attachments, hitboxes and bones intact, animation within studiomdl's
  quantisation.

## GUI shell (M2)

Install the extra and start it:

```sh
pip install -e '.[studio]'      # PySide6
valve-qc-studio [project-folder]   # or: python -m valve_qc_merger.studio
```

- **File**: new / open / open recent / close project (recent list and last
  folders in QSettings).
- **Project**: import `.mdl` files, import every `.mdl` of a folder, import
  decompiled folders; settings (studiomdl, model viewer); show the project
  folder.
- **Explorer** (left): assets grouped by kind, then builds. Context menu:
  change kind, show in folder, remove.
- **Inspector** (right): Overview (kind, folders, counts, warnings, notes),
  Bodygroups (entries in order incl. `blank`, triangles/vertices/textures),
  Textures (size, render mode, users, preview — `masked` shows index 255 as
  transparent), Sequences (fps, frames, loop, events), Bones (hierarchy with
  vertex counts), Attachments.
- **Log** (bottom) + status-bar progress and **Cancel**. Every import runs
  as a background job (`studio.jobs.JobRunner`, one at a time; actions that
  change the project are disabled meanwhile); cancelling stops the service
  at its next model.

`studio.model_info` (the Inspector's data) is Qt-free; the GUI is covered by
offscreen smoke tests (`tests/test_studio.py`, skipped without PySide6).

## Viewport (M3)

The centre of the window shows the selected asset (`studio.viewport`):

- **Playback**: sequence list, play/pause, frame slider, speed (0.1–2×).
  Frames interpolate like the engine (positions linearly, rotations by
  quaternion slerp); looping sequences wrap, others replay.
- **Bodygroups**: one selector per group with more than one entry (`blank`
  included).
- **Display**: textures, bones (lines + joints, drawn on top), attachments,
  wireframe; **Frame** (also double-click) and **First person** (the eye at
  the model origin looking down SMD -Y — what the game shows for a v_ model).
- **Mouse**: left drag orbits, right/middle drag pans, wheel zooms.

Design:

- `studio.scene` (numpy, Qt-free) stores every reference vertex in its bone's
  bind frame (each SMD's own bind, as studiomdl does), poses a sequence frame
  by FK and skins a batch (submodel x texture) with one `einsum`: ~0.5 ms per
  frame for a 50-bone, 2.4k-triangle model.
- `studio.renderer` is OpenGL 3.3 core through Qt's wrappers (no PyOpenGL):
  CPU-skinned vertex buffers streamed per frame; GoldSource render modes —
  `masked` = alpha test on palette index 255, `additive` = ONE/ONE blend
  without depth writes after the opaque pass. It sets every GL state it
  relies on at the start of each frame: Qt composites widgets in the same
  context and left the depth mask off, so `glClear` skipped the depth buffer
  and models drew see-through. GL objects are freed on the context's
  `aboutToBeDestroyed`.
- `render_offscreen(scene, state, w, h)` draws the same into a framebuffer
  object (tests, thumbnails); it needs a platform with OpenGL — Qt's
  `offscreen` plugin has none, the native one works without showing a
  window.

## Builds in the GUI (M4)

- **Build ▸ New build…** (Ctrl+B): pick the merge kind (the dialog counts the
  matching assets) and a name; merge-v builds can retarget every asset first.
- Selecting a build shows the **build panel** (right):
  - **Settings**: every asset of the accepted kinds, or only checked ones;
    the service options as a form generated from its options dataclass
    (tooltips = the CLI `--help`, `choices=` flags become drop-downs; only
    values differing from the defaults are saved to `project.toml`); for
    merge-v the retarget options. **Save**, **Save & Run**, **Compile**.
  - **Results**: status, per-part budgets (bones, bodyparts, submodels,
    sequences, textures, largest sequence stream vs studiomdl's 64K — red
    when over), every verification gate, failures.
  - **Manifest**: the per-model table (`pev_body`, hands, `anim_*`).
    **Double-click a row** to load the merged part in the viewport with that
    model's bodygroups decoded from its `pev_body` and its idle playing.
  - **Outputs**: the emitted QCs (✓ once compiled): preview in the viewport,
    open the .mdl in the configured viewer, show the folder.
- **Run** (F5) and **Compile** (F7) are background jobs; after a run the
  part figures are measured in the same job and stored in `last_run.json`
  (`studio.build_report`, Qt-free), so the panel opens instantly.

## Bone tools (M5)

Hierarchy edits on the selected asset (Inspector ▸ **Bones**), applied to every
SMD of the model (reference meshes and animations) by
`valve_qc_merger.project.bones`, Qt-free:

- **Rename…** — names only (≤ 31 characters, unique); `$attachment`,
  `$hbox` and `$controller` lines follow.
- **Change parent…** or **drag a bone onto another** (onto empty space: make
  it a root) — FK-exact, the bone keeps its world pose in every frame;
  moving a bone under its own descendant is refused.
- **Delete…** — children fold the bone's transform in and keep their pose;
  vertices it carried move to its parent and attachments are re-expressed in
  the parent's bind frame (both reported: they stop following the deleted
  bone's own motion). A root carrying vertices cannot be deleted.
- **Undo** — every edit snapshots the asset's QC + SMDs first
  (`.history/<asset>/N`, last 10 kept).

Each edit re-checks every surviving bone's world position in every frame and
logs the worst deviation (~1e-14 u on the CSO corpus). Selecting a bone
highlights it in the viewport (red cross + link to its parent).

Inspector ▸ **Attachments**: an editable table (index, bone, offset in the
bone's frame) with Add / Remove / **Save attachments** (warns above
GoldSource's 4). Attachments, joints and the highlight are drawn as 3D line
crosses — `GL_POINTS` with a shader point size draws nothing on macOS core
profile.

## Retarget (derived assets)

Explorer ▸ right-click an asset (or several) ▸ **Retarget…** (Asset ▸
Retarget…, Ctrl+R) makes a NEW asset next to the source — the source is never
touched, so the viewport can flip between before and after. In the Explorer
the new asset hangs under its source (`v_elite ▸ v_elite_hands · hands`); one
whose source sits in another category stands alone as `v_x_hands ↳ v_x`:

- **Swap hands (retarget)** → `<name>_hands`: the `retarget` service — our
  male/female hands replace the model's own, every animation is retargeted
  onto them; ready for merge-v `--shared-hands`.
- **Canonical bones (own hands)** → `<name>_canon`: the `canonicalize`
  service — the model keeps its hands; hand bones are renamed to the
  reference names, the `Bip01` root is added, the reference parentage is
  enforced and `*Nub` bones are removed (the per-model step of merge-v,
  FK-exact and pose-checked).

**Grip offsets** (swap mode): *Weapon* moves the weapon relative to BOTH
hands, in model space at the grip frame (`retarget --weapon-offset dx,dy,dz`);
*Left/Right palm* shift one palm in its own axes — x fingers-forward, y toward
the thumb, z palm normal (`--grip-offset side:dx,dy,dz`). The fingers re-snug
to the weapon after an offset, so nudge, re-run, look, repeat. The dialog is
modeless: **Apply** runs and stays open (the viewport keeps orbiting, and
reloads the result with the same camera, sequence and frame), **Run** runs
and closes. For a derived asset the viewport bar shows **Before: <source>**
(key `B`): it flips to the source model in the same camera, sequence and
frame, and back.

**Save to grip_tuning.json** stores the offsets as the weapon's tuning in
`storage/handswap/grip_tuning.json` (keyed by the source asset name =
weapon folder name; `grip_offset` per side + `weapon_offset`). Every later
retarget of a folder with that name applies them — the CLI and build-level
retarget too; explicit options still override (per side for palms, whole
vector for the weapon). A new Retarget dialog is prefilled from the table.

A derived asset stores how it was made in `project.toml`
(`[assets.derived]`: `from`, `mode`, `options`); its context menu adds
**Re-run retarget** (Ctrl+Shift+R, after the source changed) and **Retarget
settings…** (change the offsets and re-run). A re-run snapshots the previous
output, so Inspector ▸ Bones ▸ Undo restores it. A merge-v build with
**"put every view model on our hands first"** retargets only what needs it:
assets made by Retarget (swap hands) and models already wearing our hands
(bone-local match with `storage/handswap/cso_reference_hands.smd`) are taken
as they are, keeping their tuned grip; a failed retarget is listed under the
build's Failures instead of silently dropping the weapon, and a derived asset
built together with its own source is flagged as the same weapon twice.
Without that option the build mixes freely only WITHOUT `shared_hands` (each
weapon keeps its own hands); with `shared_hands` a model wearing other hands
is rejected. A derived asset goes into
builds like any other; note merge-v names manifest entries after the asset
(`v_janus1_hands`), so name it as the game should see it if that matters.

## Filter

Above the Explorer: a text filter (Ctrl+F; case-insensitive part of the
name, builds included) and a status filter — own hands, on our hands, stale,
problems in builds, in no build, made by Retarget. Matching assets keep their
parents visible (a derived asset shows under its source); empty categories
and kind groups hide; Esc clears both. The filter survives refreshes.

## Plan (merge-v)

**Save & Plan** (Build ▸ Plan selected build, F6) runs a merge-v build up to
the moment its parts are decided — staging, retarget-first, canonicalisation,
folding, the shared-hands check, the part split and bone pooling — and stops
before writing anything. The **Plan** tab lists every part with its models in
weapon order, their `pev_body` (shared hands: weapon × hand variants; per-weapon
hands only after a run), folded weapons, and every rejection with its reason
(double-click a row to select the asset). It stages under
`builds/<name>/plan/` (removed afterwards; the last run's output stays) and
records `plan.json`. On the 56-pistol corpus the plan matched the real run
for every model (part and `pev_body`).

## Deploy

Project ▸ Settings ▸ **Game folder** (the mod folder, e.g. `…/cstrike`).
**Deploy** (build panel, Explorer ▸ build ▸ Deploy to game, F8) copies the
build's compiled models — and their `<name>T.mdl` texture files — into
`models/` (player models: `models/player/<model>/<model>.mdl`; per build:
Settings ▸ *Deploy to*), plus the manifest renamed `<output name>_models.ini`
so two builds never overwrite each other's `models.ini`. It lists every file
first and marks the ones it replaces. With *deploy after every successful
compile* checked, Compile (and Run and compile) deploys by itself.

## Asset status

Every asset in the Explorer carries a dot (details in its tooltip and in
Inspector ▸ Overview ▸ Status):

| dot | meaning |
|---|---|
| red | its last build run failed or rejected it (multi-part, other hands, failed retarget) |
| orange | derived, and its source changed since (re-run it) |
| green | a view model on our hands (ready for shared-hands merges) |
| grey | a view model with its own hands (not retargeted yet) |

"On our hands" is decided by GEOMETRY, never by bone names (rigs name bones
freely — `Hand.L` is just Blender's convention): a reference SMD must be the
CSO hand mesh (`storage/handswap/cso_reference_hands.smd`) vertex for vertex
in bone space. Only meshes with exactly its triangle count are parsed, and
every verdict is cached by file time, so a refresh stays instant. The same
name-blind comparison backs merge-v's shared-hands check and the build's
"already on our hands" skip. Staleness compares the source's newest QC/SMD
edit with the time the derived asset was made (`derived.at`).

## QC editing

All in the Inspector, on the selected asset; every edit snapshots it first
(Bones ▸ Undo restores; a failed edit leaves no snapshot behind), and the
Qt-free logic lives in `valve_qc_merger.project.qc_edit`:

- **Sequences** — double-click a row: name (≤ 31 characters, unique), fps (or
  none), loop, `ACT_*` activity, and the **events** table (frame, event,
  option; 5001/5011/5021/5031 = muzzle flash on attachment 0–3, 5004 = sound).
  The block is rewritten in braced form; its animation paths and any option
  the dialog doesn't manage (`blend`, `origin`, `LX` …) are kept verbatim.
- **Textures** — double-click a texture: render mode normal / masked /
  additive / fullbright / flatshade / chrome (`$texrendermode`).
- **Skins** — the `$texturegroup` rows; picking one retextures the viewport
  (also the *skin* picker in the viewport's bodygroup row).
- **QC** — the raw text with highlighting; **Save QC** writes it only if the
  model still loads and references no missing file (otherwise the old text
  is put back and the reason shown); **Revert** drops unsaved edits.

## Packaging (M6)

```bat
rem Windows (from the repo root; Python 3.11+ only for the build)
tools\build_studio.bat
```
```sh
sh tools/build_studio.sh        # macOS / Linux
```

`tools/studio.spec` builds **one folder** `dist/valve-qc-studio/` (Windows:
`valve-qc-studio.exe` inside — ship the whole folder; macOS also
`dist/valve-qc-studio.app`). One-folder because a one-file build would
unpack ~150 MB of Qt on every start. It is a windowed app (no console),
bundles `storage/hands`, `storage/handswap` and `storage/players_donor`
(the services read them via `resources.data_root()`, which is the bundle
when frozen) and excludes the Qt modules the studio never imports
(~163 MB: Qt ~87 MB, data ~44 MB).

`valve-qc-studio --selftest [model.mdl]` is the headless smoke test the
build scripts and CI run: the window starts on Qt's offscreen platform, a
project is created, the model is imported with the in-process decompiler
and turned into a viewport scene, and the bundled data is checked; exit
code 0 = healthy (a windowed exe prints nothing, the exit code tells).

On a machine without OpenGL 3.3 the viewport says so (with the reason)
instead of staying blank; everything else works.

CI (`.github/workflows/`):

- `tests.yml` — ruff + the whole pytest suite on Windows, Linux and macOS on
  every push / pull request (GUI tests offscreen; the real GL render skips
  where no OpenGL is available).
- `build-exe.yml` — on dispatch / `v*` tags: the CLI one-file exe (smoke:
  `--version`, `merge-p`, `decompile`) and the studio folder (build +
  selftest), as artifacts `valve-qc-merger-<os>` / `valve-qc-studio-<os>`.

The CLI exe now bundles the same `storage/` data, so `retarget` and
`merge-players` work from any folder (they looked for `storage/` next to
`pyproject.toml`, i.e. only inside a checkout).
