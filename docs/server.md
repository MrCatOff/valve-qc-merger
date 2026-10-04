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
  or map that cannot be read; a model or sprite a map's entity uses that no
  folder has;
- **warnings**: upper case in a path (the Linux server is case-sensitive),
  a sound referenced with another case than the file, non-ASCII or spaces,
  textures over 512 px, WAV files the engine mangles (not 8/16-bit PCM,
  stereo, a rate other than 11025/22050/44100, clipping); a sound a map's
  entity plays, or a texture WAD its worldspawn lists (`wad` key), that no
  folder has — clients then see missing textures unless the map embeds them;
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
- `rechecker/resources.ini` — rules for
  [ReChecker](https://github.com/rehlds/ReChecker): every packed model and
  sprite with its true hash (the first 8 hex digits of its MD5) accepted
  (`IGNORE`) and any other copy kicked (`UNKNOWN … "kick [userid] …"
  BREAK`) — a modified model or sprite (see-through walls, glowing players)
  is the classic cheat this stops. Append it to ReChecker's `resources.ini`.
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

## Starting and filling a project

- **File ▸ New project** starts from a template: *Empty*; *Classic weapons*
  (categories pistols, rifles, smgs, shotguns, snipers, machine guns, knives,
  grenades — Explorer ▸ a category ▸ *Create builds* once it has models);
  *Zombie server* (categories *weapons* and *zombie hands*, a zombie-hands
  build, and weapon builds: view on our hands, player-held, world; client
  sounds through `precache_generic`).
- **Project ▸ Import server folder…** brings a mod folder in as one job:
  every model of `models/` (decompiled in the app, one by one — names already
  in the project are skipped, a broken model is reported and the rest goes
  on), the sounds of `sound/` (by default only those the imported models
  play — a stock folder holds thousands — or all, or none) and the weapon
  HUDs of `sprites/` (`weapon_*.txt` and the sheets they draw from), into a
  category of your choice. `valve_qc_merger.project.workflow` does it.
- The Explorer filter (Ctrl+F) searches sounds and sprites too.

## Unprecache stock models

Stock models ReGameDLL precaches stay in the 512 even when no player ever
sees them (your plugin gives every weapon its own view model, dropped
weapons become a supply box, a skin plugin replaces player models). The
Server window's **Unprecache** tab lists them by group — view models,
player-held, world, the tactical shield, player models, shell casings —
checked against the game folder; tick what your server replaces.

**Replace them with** (recommended) names one model that takes their place
wherever the game still sets them (e.g. `models/w_supplybox.mdl` for every
dropped weapon); blocking without a replacement crashes clients on any
entity that still uses the model. The tab shows the slots freed (the
replacement costs one unless it is precached anyway), the budget counts
them, and **Export list.ini…** writes the file for
[Metamod Unprecacher](https://github.com/In-line/metamod_unprecacher)
(`addons/unprecacher/list.ini`; `path c replace_path` per line).

## Config (game.cfg, server.cfg)

The Server window's **Config** tab edits `game.cfg` (ReGameDLL) and
`server.cfg` (ReHLDS) of the game folder, or any cfg you pick. Every cvar is
listed with its value, its default and its description, options and notes
— taken from the file's own comments, else from the references bundled in
`storage/server/` (ReGameDLL's `dist/game.cfg` and the cvar list of the
ReHLDS README, both MIT), so a stripped file is still documented. Search,
filter by group (`mp_`, `sv_`, `bot_`) or *Only non-default*; non-default
values are blue, unsaved edits orange.

**Save** changes only the edited lines (a cvar the file does not set is
appended with its description); comments, `exec`/`echo` lines and anything
unknown stay, and the old file is kept as `<name>.bak`. A new `game.cfg`
starts as the documented reference with your values. Passwords
(`sv_password`, `rcon_password`) are never listed. `valve_qc_merger.server.cfg`
does the parsing and writing.

## Weapon previews

**Project ▸ Weapon previews…** draws a picture of every view model (or one
category's) for your players — menus, a MOTD, a forum post. The v_ model is
the source: every weapon has one (knives have no w_), and it is far more
detailed than the p_. The hands go first — by bodygroup (`hands`, `arms`),
by texture name (`hand`, `glove`, `sleeve`…), by the stock CSO hand meshes
(their triangle counts give them away even in a decompile that lost every
name), else by bone (hand/finger/arm bones and their chains) — then the
weapon is posed on the first frame of its idle and turned to show its right
side: barrel horizontal and pointing right (or left, an option), a knife
held upright laid down, pieces the idle parks away from the weapon (a speed
loader under the camera, CSO's giant hidden planes) left out, a pair of
pistols aimed by one of them. Additive parts (an ice blade, glows) are added
on top unlit, as the engine does. A weapon that *is* the hands (zombie
claws, a gauntlet) is drawn as it is and listed in the log.

Into the chosen folder (default `<project>/previews`):

- `images/<weapon>.png` — transparent, supersampled, named without `v_`;
- `sheet.png` — a grid of cards with the names (columns of your choice);
- `index.html` — a self-contained page (no scripts) with a card per weapon
  and the asset's **Notes** as its description, a section per category —
  host it next to `images/` and point a MOTD or `say` link at it;
- `weapons.txt` — `name|category|image|description` per weapon, for plugins.

`valve_qc_merger.preview` renders in software (numpy, no GPU), so the
result is the same on every machine.

## Sprays and WADs

**Asset ▸ New spray (tempdecal.wad)…** turns an image into the spray file
a player's client sends: one `{LOGO` texture (lump type `0x40`), 255
colours, palette index 255 (blue) where the image is transparent. Sizes keep
the image's aspect with both sides a multiple of 16, at most 256, and 14336
pixels in all (112 × 128 and the like — the client refuses bigger); an image
is not scaled up unless you pick that. Stock HLDS takes only 64 × 64 logos
(offered too); ReHLDS takes the bigger ones with
`sv_rehlds_allow_large_sprays 1`, its default. The file goes into `cstrike/`
(make it read-only, or the game overwrites it with the Options spray).

**Asset ▸ Open WAD…** shows every texture of a WAD3 — map textures,
`decals.wad` (drawn as the engine does: the index is the opacity, the last
palette colour the colour), sprays — with a filter and *Export PNG…*.
`valve_qc_merger.sprite.wad` reads and writes WAD3 (with the three mip
levels) and makes sprays without Qt.

## Map entities

The Server window's **Entities** tab opens a map of the game folder's
`maps/` (or any BSP): its entity classes with counts, the entities of a
class (origin, model / name), and the keys of one — values editable, keys
added or removed.

- **Remove** drops the selected entities (never `worldspawn`).
- **Replace…** puts something else in their place, keeping `origin` and
  `angles`: a model of your own (`cycler_sprite` — drawn, not picked up;
  e.g. `models/w_supplybox.mdl` where a ZM map had dropped-weapon spawns,
  `armoury_entity`), a marker for a plugin (`info_target` with a
  `targetname`, e.g. `vqm_supplybox`, where a plugin spawns its supply
  boxes), or nothing. The status line counts the model slots a change adds.
- **Save .ent** writes `maps/<map>.ent`: ReHLDS reads it instead of the
  map's own entities with `sv_use_entity_file 1` — the BSP is untouched.
  When a `.ent` exists the tab opens it, as the server would.
- **Save BSP…** rewrites the entity lump (over the map: the original is kept
  as `<map>.bsp.bak`). Every other lump is copied byte for byte, and the
  engine's map CRC skips the entity lump, so players who already have the
  original map still join.

`valve_qc_merger.server.entities` does the reading and writing without Qt.
