# Changelog

All notable changes to valve-qc-merger and Valve QC Studio. Each release's
downloads (CLI, Studio, studiomdl for Windows, macOS and Linux) are on its
[GitHub release](https://github.com/MrCatOff/valve-qc-merger/releases).

## [Unreleased]

### Maps
- **`valve-qc-merger map check MAP… [--game DIR] [--json OUT]`**: what would
  break a compiled map (BSP v30) on a server, with the operation that cures
  each issue. Critical: models / sprites in no folder, models not version 10,
  WADs or textures not found, lit faces past 256 texels ("Bad surface
  extents"), geometry past ±4096, too few spawns for a full server. Warnings:
  spawns under the profile's count (`zp`: 48 a team) or in a wall (hull 1),
  classes CS 1.6 drops (`particle_emitter`), sounds not found, names only a
  case-insensitive file system finds, textures over 256, model / sound slots
  near 512 with ReGameDLL's own. Rules and profiles from a JSON file
  (`configs/mapkit.json`). Exit code 0 / 1 / 2 / 3.
- **`map fix MAP [--spawns 48] [--markers zp_supply:24] [--ent] [--out DIR]`**:
  fixes without recompiling (the CRC stays): CSO classes removed (brush ones
  kept as `func_wall`), entities with missing models removed or the files
  copied from `--source`, names put in the disk's case, stuck spawns moved,
  spawns generated up to the count a team (reachable on foot, through water
  and up ladders; apart; facing open space; the same every run), plugin
  markers, the WAD list tidied, `.res` written; the BSP (with a `.bak`) or a
  ReHLDS `.ent`. All 8 maps of the CSO ZM pack: 48 + 48 spawns, no critical
  issue, CRC unchanged.
- **`map textures MAP list | replace NAME IMAGE | embed | extract OUT.wad
  [--strip] | prune-wads`** and **`map shared-wad MAPS --out pack.wad`**:
  texture replacement at the texture's own size (PNG / TGA / BMP read
  without Pillow; `{` textures keep see-through), embedded or in a copy of
  its WAD; embedding from the map's WADs, extraction to a WAD; one WAD for a
  pack with duplicates kept once and name conflicts reported. Changes the
  CRC → the map is saved as `<map>_sp`; every other lump checked byte for
  byte after writing.
- **`map props MAPS [--shared NAME] [--gc]`**: a map's prop models
  (`cycler_sprite`, `env_sprite`) merged with merge-props into one model,
  compiled, each checked against its original (first frame within 0.05
  units), named `models/<map>/<name>_<hash8>.mdl` so clients re-download a
  changed set; entities get `model` / `body` / `sequence`, the CRC stays. The
  CSO ZM pack: 16 → 1, 14 → 1, 7 → 1, 20 → 2 model slots.
- **`map preview MAP --out plan.png`**: the map from above in its textures'
  colours, shaded by height, with the spawns and where they face.
- New `mapkit` package (no Qt): BSP v30 read / write lump by lump (an
  unchanged map writes back byte for byte, a resized lump moves the ones
  after it), the engine's map CRC, faces, surface extents, hull 0-3 point
  contents, hull traces as the engine's. Server ▸ Maps / Budget read maps
  through it.

## [0.22.0] - 2026-11-08

### Textures
- **Exact texel UVs.** studiomdl reads an SMD UV as `int(u * (width - 1))`;
  the decompiler wrote `s / width`, so every texel but the first came back one
  short and UV islands slid onto the texture's black background — the dark
  seams on hands (Valve's v_knife: 938 of 1224 finger vertices on another
  texel). The decompiler now writes the texel's middle on studiomdl's scale:
  all 31 Valve view models round-trip to the very same texels.
- Our CSO hands asset (every retarget's hands) and storage's hand and player
  SMDs were a texel off too: rewritten.
- Older projects are fixed when opened (project format 3), and imported
  decompiled folders on import, when their UVs name whole texels (another
  tool's artist UVs are left alone).
- The viewport and renders sample textures on the texels the game draws (no
  specks at UV seams the compiled model does not have).

### Retarget
- An arm the original keeps out of view stays out of view: v_c4's draw rises
  from below the screen, and our forearm and upper arm were already up in it.
  The retarget check counts arms in view while the original's are not
  (73 nexon models: 164 → 39 frames; v_c4: 5 → 0).
- Retarget revision 7: swap-hands results are re-made by the next build.

### Projects and builds
- **Server folder** (Project ▸ Settings): every build puts its models in
  `models/<folder>/` and the sounds it ships in `sound/<folder>/…` (sound
  events rewritten when staged); the game's own sounds stay where they are,
  player models stay in `models/player/`.
- **One skin count per weapon**: a staged p_ / w_ model gets as many skin
  rows as its v_ (and its w_ / p_ sibling) — the sibling's real variants over
  the same textures first (w_dragonsword gets p_dragonsword's second skin),
  then its first row again — so every `<model>_skin<k>` exists in the v_, p_
  and w_ manifests.
- A build's server files no longer ship the sounds the game install has (the
  server package already did not).

### Studio
- **Asset ▸ Sounds of this model…**: every sound the weapon's events play
  (sequences, frames) with a picker over the library and the game's sounds,
  play before and after, import a WAV; saving replaces the sound in every
  sequence (Undo restores). No QC editing.
- The sequence dialog's sound events use the same picker; *Add a sound at
  frame N* adds one on the frame the viewport shows.

## [0.21.0] - 2026-10-12

- **Game install** (Project ▸ Settings, read only): a clean game's sounds
  (`cstrike/sound` and Half-Life's `valve/sound`) take part in Find similar
  sounds, a game sound kept first; the server package precaches but never
  ships them and warns about a sound of yours under a game sound's name.
  `valve-qc-merger project sounds PROJECT --game-install … [--similar] [--share]`.
- Find similar sounds about 5x faster, with progress.
- merge-zhands: body 2 draws nothing (as merge-v in 0.20.0). **pev_body
  values change** — rebuild and take them from models.ini.

## [0.20.0] - 2026-10-12

- merge-v: body 2 draws nothing — CS's client draws view model body 2 by
  itself (fire events, the post-prediction fix-up, `cl_lw 1`); the low-order
  group keeps a second blank at index 2. **pev_body values change**; shared
  hands `pev_body = (weapon + 1) × 4 + 1`, second hands `+2`.
- `vqm_resources.inc` number tables wrapped (amxxpc refused long lines).
- Release builds are attached to the GitHub release.

## [0.19.0] - 2026-10-12

- merge-zhands: **one frog for all** — every zombie's grenade animation is
  redrawn for one frog of one size (mirrored frogs included);
  `--own-grenades` keeps a frog per rig.
- merge-zhands: **grenade skins** (`--grenade-skin BMP`) as extra grenade
  entries; models.ini gives `pev_body_<skin>`.

## [0.18.0] - 2026-10-12

- **Retarget check**: every swap-hands result is checked against its original
  frame by frame (weapon moved, arms jumping, hands off the grip, shoulder in
  view); Asset ▸ Check retargets, Compare with original…, `project audit`.
- Retarget: weapon parts on old arm bones stay put; arms turn no faster than
  the original's; Valve's v_famas / v_knife / v_shield_r handled. Revision 5.
- merge-zhands: one grenade entry per grenade rig.
- CI: releases built only after the full 3-OS test suite.

## [0.17.0] - 2026-10-11

- Retarget: shoulders stay off screen (the upper arm bends at the elbow, at
  most 60°, until the shoulder leaves a widescreen view).
- Swap-hands assets record their retarget revision; builds re-make older ones.

## [0.16.0] - 2026-10-11

- **Workers** (Settings ▸ Workers): retarget, import, normalise and compile
  run in parallel processes; a 540-model server imports in ~1 min.
- Import: categories from source folders, packs told by content, sound event
  paths normalised.
- Retarget: hand sides by the hand's own shape; worn weapons not swapped;
  results over 127 bones not used.
- merge-zhands split in minutes with progress; many merge fixes; big projects
  open in seconds.

## [0.15.0] - 2026-10-10

- **studiomdl with 1024 submodels** and a 16 MB output check, for Windows,
  macOS and Linux; builds read the compiler's limit.
- pev_body limits from the server (`delta.lst`); parts close under 16 MB.
- View models: every bodygroup leads with a blank (body 0 draws nothing).
- merge-players conformed to the donor skeleton; merge-zhands rigs pooled.

## [0.14.0] - 2026-10-10

- Import a whole server: portable names, placeholders for missing textures.
- **merge-props** for effects, projectiles and props.
- Packs unpacked by content; "hands are the model" view models go to a
  no-hands part; zombie claws with a shared grenade.

## [0.13.0] - 2026-10-08

- Server files per build (`builds/<name>/cstrike/`), Deploy copies them.
- QC preflight before studiomdl; `valve-qc-merger project …` command line;
  incremental compile.
- ReAPI weapon plugin `vqm_weapons.sma` in the server package.

## [0.12.0] - 2026-10-07

- Heavy jobs run in their own process; viewport fixes for Windows / NVIDIA;
  Help ▸ Graphics report.

## [0.11.0] - 2026-10-06

- Project ▸ **Find similar sounds** (identical, same sound saved another
  way, similar) with sharing by aliases.
- One skin name across v_ / p_ / w_ manifests (`<model>_skin<k>`).

## [0.10.0] - 2026-10-06

- Builds ship every model (rejected ones on their own); sounds others hear go
  through `precache_sound`; Budget with measured ReGameDLL counts; levelled
  weapon previews.

## [0.9.0] - 2026-10-05

- Budget: as imported vs after merge, ReGameDLL's real precache; smoother
  imports; retargets saved as `<name>_hands` assets.

## [0.8.0] - 2026-10-05

- Server tools: Maps, Download, Logs; package updates without client
  conflicts (versioned names, `update/`).

## [0.7.0] - 2026-10-04

- Server tools: Unprecache, Config, Entities, FastDL; weapon previews; sprays
  and WAD browser; ReChecker rules.

## [0.6.0] - 2026-10-04

- Server budget and doctor, sound library and fixes, server package, sprites
  and weapon HUDs.

## [0.5.0] - 2026-10-04

- Redesigned Studio (dark theme, toolbar, viewport, first person as in the
  game, inspector, readable forms); merge fixes.

## [0.4.0] - 2026-10-03

- Zombie grenades (`zhands-grenade`); retarget quality measured on 174
  native weapons.

## [0.3.0] - 2026-10-03

- Valve QC Studio; pure-Python retarget engine; merge-players and
  merge-zhands; merge-v shared hands; commands renamed merge-v / merge-p /
  merge-w.

## [0.2.0] - 2026-08-02

- Blender retargeting pipeline; GoldSrc model decompiler; QC parser for both
  dialects.

## [0.1.0] - 2026-08-01

- First release: merge-view, merge-player, merge-world, retarget.

[0.22.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.22.0
[0.21.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.21.0
[0.20.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.20.0
[0.19.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.19.0
[0.18.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.18.0
[0.17.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.17.0
[0.16.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.16.0
[0.15.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.15.0
[0.14.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.14.0
[0.13.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.13.0
[0.12.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.12.0
[0.11.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.11.0
[0.10.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.10.0
[0.9.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.9.0
[0.8.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.8.0
[0.7.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.7.0
[0.6.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.6.0
[0.5.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.5.0
[0.4.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.4.0
[0.3.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.3.0
[0.2.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.2.0
[0.1.0]: https://github.com/MrCatOff/valve-qc-merger/releases/tag/v0.1.0
