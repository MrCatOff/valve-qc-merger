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
