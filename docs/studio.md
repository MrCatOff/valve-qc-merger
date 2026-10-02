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
| M4 | builds in the GUI: options forms, budgets, gates, compile, manifest | planned |
| M5 | bone tools: rename / reparent / delete, attachments, weapon seating | planned |
| M6 | packaging: windowed PyInstaller exe + CI | planned |

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
format = 1

[settings]
studiomdl = "C:/tools/studiomdl.exe"

[[assets]]
name = "v_deagle"
kind = "v"
path = "assets/v/v_deagle"
source = "D:/dump/v_deagle"
notes = ""

[[builds]]
name = "pistols"
kind = "merge-v"          # merge-v | merge-p | merge-w | merge-players | merge-zhands
assets = []               # empty: every asset of the kinds this build accepts
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
