# Server tools (ReHLDS + ReGameDLL + ReAPI)

The studio targets servers on **ReHLDS + ReGameDLL + ReAPI**. Project ▸
**Server budget & doctor…** (Ctrl+Shift+S, toolbar *Server*) opens one window
with two tabs.

## Limits

| Precache | Slots | What takes one |
|---|---|---|
| Models | 512 | every `.mdl`, every `.spr`, every brush model of the current map (the world included) |
| Sounds | 512 | `precache_sound` + the map's entity sounds |
| Generic | 4096 (ReHLDS; stock HLDS 512) | `precache_generic`: download-only files, e.g. the client sounds view models play |
| Events | 256 | `events/*.sc` |
| Resources | 1280 | everything a client is told to download |
| Path | 63 characters | every precache name (`models/…`, `sound/…`) |

Models and sounds stay at 512 on every server: they are the client's tables
(also after Valve's HL25 update).

## Budget

Pick a map (from the game folder of Project ▸ Settings, `maps/*.bsp`, read
directly: brush models + entity models/sprites/sounds) and see the slots it
takes together with this project's compiled builds and the sounds their view
models play (event 5004). The studio cannot see what ReGameDLL and the AMXX
plugins precache: enter those counts once (saved in the project). Choose
where your weapon plugin puts the view models' client sounds — on ReHLDS
`precache_generic` (4096 slots) keeps them off the 512 sounds. Bars turn
amber past 90 % and red over the limit; *Every map* lists them all. The line
under the bars tells how many model slots merging saved (inputs → parts per
build).

## Doctor

Scans a mod folder (default: the game folder) — `models`, `sprites`,
`sound`, `maps`, `gfx`, `events` — and lists, worst first:

- **errors**: a `T.mdl` / `01.mdl` companion a model needs is missing; a
  sound a model plays (events 5004/1004/1008) is in neither the folder, the
  `<mod>_downloads` folder nor `valve`; a path over 63 characters; a model
  that cannot be read;
- **warnings**: upper case in a path (the Linux server is case-sensitive),
  a sound referenced with another case than the file, non-ASCII or spaces,
  textures over 512 px, WAV files the engine mangles (not 8/16-bit PCM,
  stereo, a rate other than 11025/22050/44100, clipping);
- **info**: identical files (one could serve both).

Filter by severity or text, double-click a row to show the file, *Copy
report* for the shown rows. `valve_qc_merger.server` is Qt-free (`bsp`,
`scan`, `doctor`, `budget`, `limits`).

## Sounds

The project keeps a **sound library** in `<project>/sounds/`, mirroring the
game's `sound/`: a sound is named by its path there (`weapons/ak47_clipin.wav`),
the name model events (5004) and plugins use. Project ▸ **Import sounds…** /
**Import sound folder…** copy WAV files in: the path after a `sound` folder
is kept (`…/cstrike/sound/weapons/x.wav` → `weapons/x.wav`); a plain folder
keeps its own name (`…/zombie/hit/claw.wav` → `zombie/hit/claw.wav`).

The Explorer lists them under **Sounds** as folders; a ▲ marks a file the
engine would mangle. Selecting one shows the **sound panel**: waveform (loop
cue points dashed), format, channels, rate, bits, length, peak, loop, size,
the engine check, and the project's models that play it (click to open).
**Play / Stop** use the system player (Windows `winsound`, macOS `afplay`,
Linux `paplay`/`aplay`: nothing extra in the build).

**Fix…** (one sound, a folder, or all — Explorer context menu) rewrites the
file as 16-bit PCM: mono (world sounds must be), an engine rate (keep a valid
one, else the nearest higher of 11025/22050/44100, or force one), optionally
normalised to a peak (e.g. −1 dBFS) and trimmed of leading/trailing silence
(never a looping sound). Cue points move with resampling. The original stays
in `.history/sounds/` until **Undo fix**.

**Sound events with the animation**: a view model's sound events show as
amber ticks on the timeline, and while a sequence plays each event's sound
plays on its frame (looping wraps included). Files come from the library,
then the game folder, `<mod>_downloads` and `valve` (case-insensitive). The
speaker button in the viewport's bar turns event sounds off.

`valve_qc_merger.sound.wav` (read/check/fix, Qt-free) and
`valve_qc_merger.project.sounds` (the library) do the work.

## Server package

Build ▸ **Export server package…** (Ctrl+Shift+E) writes
`<folder>/<project>_server_package/`:

- `cstrike/` — every compiled build (models + `T.mdl`, manifests renamed
  `<name>_models.<ext>`) where Deploy would put them, plus every sound the
  models' events play, taken from the library or the game folder, under
  `sound/`. The same tree is what FastDL (`sv_downloadurl`) serves: upload it
  to the server and to the HTTP host as is.
- `amxx/vqm_resources.inc` — for a ReAPI weapon plugin: `VQM_MODELS` and
  `VQM_CLIENT_SOUNDS`, a `vqm_precache()` to call from `plugin_precache()`
  (client sounds through `precache_generic` — ReHLDS's 4096 slots — or
  `precache_sound`, following the Server window's choice), and per merged
  weapon `VQM_<WEAPON>_MODEL`, `_BODY` (its `pev_body`), `_SKIN` and
  `_ANIM_<SEQUENCE>` numbers from the manifest.
- `vqm_resources.res` — the same files as a `.res` list (copy as
  `maps/<map>.res` for a map to send them).
- `package_report.txt` — files and sizes, what a new player downloads,
  sounds the models play that nothing provides, builds left out (not run or
  not compiled).

## Sprites and weapon HUDs

The project's **sprite library** (`<project>/sprites/`, mirroring the game's
`sprites/`) shows under **Sprites** in the Explorer. Asset ▸ **Import
sprites…** copies `.spr` / `.txt` files in (the path after `sprites/` kept).

- **The sprite panel**: animated preview (frames, play), type (how it faces
  the viewer), texture format and what it means, size, frame count. A HUD
  `.txt` shows its entries and the icon each cuts from its sheet.
- **New sprite from images…**: PNG/BMP/TGA/JPG frames (same size) become one
  sprite with one 256-colour palette (exact when the images use few
  colours, else median cut). Formats: *normal* (opaque), *additive* (glows,
  muzzle flashes, HUD icons: black is invisible), *indexalpha* (one colour
  with the image's transparency: smoke, decals), *alphatest* (cut-out: index
  255 is a hole).
- **New weapon HUD…**: the weapon-list icon (fitted to 170×45), an optional
  selected icon and an optional 24×24 ammo icon are packed into one additive
  256-wide sheet `sprites/640hud_<weapon>.spr`, and `sprites/weapon_<name>.txt`
  is written (320 and 640 entries on the same pixels, the stock crosshair).

Budget and package know the difference: effect sprites take **model**
slots (`precache_model`, `VQM_SPRITES`); weapon HUD files (`weapon_*.txt`
and the library sheets they use) are download-only (**generic**,
`precache_generic`, `VQM_HUD_FILES`). The package copies the library into
`cstrike/sprites/`. `valve_qc_merger.sprite` (`spr`, `hud`) is Qt-free.
