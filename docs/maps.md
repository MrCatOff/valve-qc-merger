# Maps (`map`)

Tools for compiled GoldSource maps (BSP v30) on a Counter-Strike 1.6 server
(ReHLDS + ReGameDLL + ReAPI). They work on the compiled map: no brush editing,
no compiler of their own. The code lives in `valve_qc_merger.mapkit` (numpy, no
Qt), so the same checks run from the command line, in tests and in Studio.

```bash
valve-qc-merger map check   MAP.bsp|FOLDER ... [--game DIR] [--json OUT] [--profile zp] [--rules FILE] [--all]
valve-qc-merger map preview MAP.bsp --out plan.png [--size 1024] [--game DIR] [--no-spawns] [--zmin Z] [--zmax Z]
```

## Where files are looked up

The engine's way: the map's own game folder (the one above `maps/`), then
every `--game` folder (each with its `_downloads` folder), then `valve` next to
any of them. Pass a clean game, e.g.
`--game "~/Library/Application Support/Steam/steamapps/common/Half-Life/cstrike"`:
then Half-Life's own files (`valve/models/forklift.mdl`, `halflife.wad`,
`sprites/fire.spr`) count as present, as on a real server. Lookups ignore case
the way a Windows client does, and a file spelled in another case is reported
(a Linux server does not find `models/D_tree01.mdl` as `d_tree01.mdl`).

## `map check`

One line per map, then its issues. Exit code: 0 no issues, 1 warnings,
2 critical issues, 3 a map could not be read.

| Level | Code | When |
|---|---|---|
| critical | `missing_model` | a `.mdl` / `.spr` an entity names is in no folder: the server stops loading the map |
| critical | `model_version` | a model is not studio version 10 (CS 1.6 loads only 10) |
| critical | `missing_wad`, `missing_texture` | a WAD of worldspawn's `wad` is not found; a texture is neither embedded nor in a WAD found |
| critical | `bad_extents` | a lit face (no `TEX_SPECIAL`) wider than 256 texels, rounded out to 16 as the engine does: "Bad surface extents" |
| critical / warning | `world_bounds` | geometry past ±4096 / within 64 units of it |
| critical / warning | `few_spawns` | a team has fewer spawns than half of `max_players` / than the profile's `min_spawns` (CT = `info_player_start`, T = `info_player_deathmatch`) |
| warning | `spawn_in_wall` | a standing player does not fit at a spawn (origin + 1, hull 1) |
| warning | `unknown_class` | a class ReGameDLL does not link (`particle_emitter` …): dropped with "Can't init" |
| warning | `missing_sound` | an entity's `.wav` is in no folder (it plays nothing) |
| warning | `case_mismatch` | a file found only in another case |
| warning / critical | `texture_size` | a texture side over 256 / over 512 |
| warning | `texture_not_16`, `texture_animation` | a side not a multiple of 16; `+1name` without `+0name` |
| warning | `model_slots`, `sound_slots` | the map's slots with what ReGameDLL precaches (169 models, 293 sounds) leave under 10 % of 512 |
| warning | `entities`, `leafs`, `no_vis`, `no_light`, `missing_sky`, `missing_detail` | past 80 % of the edicts (900 + 15 × (players − 1)), over 8192 leafs, no visibility or light data, sky sides or detail textures not found |
| info | `unused_wad`, `summary` | a WAD no texture comes from; what the map is |

Model slots: the map itself, each brush model (`*1` …) and each distinct
`.mdl` / `.spr` an entity names. `--json` writes the report (one object, or a
list for several maps) with `map`, `file`, `version`, `size`, `crc` (the
engine's map CRC), `bounds`, `counts`, `slots`, `spawns`, `classes`, `wads`,
`skyname` and `issues` (`level`, `code`, `text`, `where`, `fix` — the operation
that will cure it).

### Rules

`--rules FILE` (see [`configs/mapkit.json`](../configs/mapkit.json)) overrides
any threshold, globally or per profile; `--profile` picks one. The default
profile `zp` is a 32-player Zombie Plague server wanting 48 spawns a team.

## `map preview`

The map from above: floors and slopes (sky, triggers and clips left out) in
their textures' average colours (embedded, or from the map's WADs — pass
`--game` for the stock ones; grey when not found), shaded by height, the
highest floor on top; a 512-unit grid; CT spawns blue and T spawns red with an
arrow where they face. `--zmin` / `--zmax` keep one storey.

## Maps and the CRC

A client compares its copy of a map with the server's by a CRC of every lump
but the entities. An entity edit keeps it (clients with the original still
join); any other change needs a new map name (`<map>_sp`). Worldspawn's `wad`
is read by the client from its own copy, so a change players should see there
needs a new name too.
