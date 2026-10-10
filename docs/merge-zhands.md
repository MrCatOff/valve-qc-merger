# merge-zhands

Merge CSO zombie hand view models into ONE model: every zombie's claws as a
`hands` bodygroup and the frog grenade as a single shared `grenade`
bodygroup.

```sh
# 1. decompile the .mdl files yourself (the command never calls a decompiler)
for f in tmp/zombie-hands/*.mdl; do tools/decompmdl "$f" tmp/zombie-hands-decompiled; done

# 2. merge
python -m valve_qc_merger merge-zhands tmp/zombie-hands-decompiled --out out/zhands
```

## Inputs

One decompiled model per subdirectory, named
`v_<zombie>_<knife|grenade>[_<variant>]`:

- `v_<zombie>_knife` — bare claws (some carry an extra mesh: heavy's blade,
  sting finger's syringe).
- `v_<zombie>_grenade` — the same claws holding the frog grenade.
- `v_<zombie>_knife_<variant>` — e.g. `v_ghost_knife_alternate`, the same
  claws with `$texrendermode ... additive` (the ghost's invisibility skill).

Non-ASCII texture names are renamed to ASCII (as in merge-v). If the
decompiler could not write such a texture at all (macOS refuses non-UTF-8
file names — e.g. voodoo's `Èú·¯_copyright...bmp`), extract it from the .mdl
into `maps_8bit/` under the name the SMDs use before merging.

## Output

```
out/zhands/
  v_zhands.qc      $bodygroup "grenade" { blank, grenade } + $bodygroup "hands" { ... }
  hands/<entry>.smd   one per distinct hand mesh
  grenade/grenade.smd the shared grenade
  v_<zombie>_<role>/  that model's sequences
  *.bmp, models.ini
```

`pev_body = grenade_on + 2 * hands_index`: the grenade group comes first, so
it is the low bit — a zombie's knife and grenade are `n` and `n | 1` (the
merge-v `--shared-hands` convention). `models.ini` gives each
input model its `pev_body`, its `hands` entry and every `anim_<name>` index;
the knife and grenade weapons of one zombie use the same .mdl with different
`pev_body` values.

## How it merges

1. **Split** every mesh into hand triangles and grenade triangles (texture
   prefix `--grenade-prefix`, default `frogbomb`).
2. **Namespace the grenade subtree** (`Bone_Root`/`bomb_Root` and everything
   under it) as `gren_*`: the rigs disagree on the root's name, and the ghost
   knife rig uses `Bone_Root` as its hand root.
3. **Canonical hand bones**: every rig is renamed onto the first knife
   model's bone names with the merge-v correspondence engine (sting finger's
   `Bip01 L...` arms and voodoo's `NEXON_CSO_...` right hand become
   `Bone01...`), restricted to the bones of the dominant hand texture so an
   extra under the wrist (blade, syringe) is not taken for a sixth finger. A
   rig that already has every reference bone under the same parent is left
   untouched. The 13 CSO models merge into 96 bones.
4. **One node table** (merge-v `unify_skeletons`): every mesh keeps its own
   bind, every sequence is re-solved exactly per frame.
5. **Dedupe hands**: knife and grenade hands that are the same mesh
   (triangles, bones, render modes; positions in each bone's own bind frame
   and UVs within 0.02) become one entry — banshee and stamper here.
6. **One grenade**: taken from a model using `--grenade-texture` (default
   `frogbomb.bmp`, else the most common grenade); every grenade sequence
   drives it. Models whose own grenade differs (ghost and voodoo ship
   `frogbomb_new`) are listed as warnings.
7. **Textures** are staged by (content, render mode): one texture used both
   opaque and additive gets a second copy (`..._additive.bmp`).
8. **Sequences** are named `<zombie>_<role>_<seq>` (≤ 31 chars) and deduped by
   content + fps + events + `loop`.

## Verification

The command re-reads what it wrote: one node table across every SMD, and
every mapped bone's world position matching the original animation (first,
middle and last frame) within 2e-3 units. On the 13 CSO models, a compiled
v_zhands.mdl (4 MB, stock studiomdl) decompiled back renders every hand
within 0.045u of its original in every sequence, and the shared grenade
within 0.08u (ghost: 0.6u, its own grenade is a different model).

## Options

| Option | Meaning |
| --- | --- |
| `--out DIR` | output directory (required) |
| `--name STEM` | model name (default `v_zhands`) |
| `--exclude NAME` | skip a model directory (repeatable) |
| `--grenade-prefix TEXT` | texture prefix marking grenade triangles (default `frogbomb`) |
| `--grenade-texture BMP` | take the shared grenade from a model using this texture (default `frogbomb.bmp`) |
| `--manifest-format` | `ini` (default), `json` or `toml` |

## Zombie hands without a grenade: `zhands-grenade`

A zombie that ships only `v_<zombie>_knife` gets its grenade generated: the
bundled donor (`storage/zhands/grenade_donor/v_banshee_grenade` — frog bomb +
`idle`/`pullpin`/`throw`/`deploy`, community-made banshee hands) has its hands
swapped for the zombie's with the retarget engine. The zombie's hands come
from the knife model as-is (own textures and `$texrendermode`, so a ghost
`alternate` stays additive); extra props are left out — the heavy's blade,
the voodoo doll, triangles bridging the two arms; a syringe sharing the hand
mesh (sting finger) is split off by bone.

```bash
python -m valve_qc_merger zhands-grenade --knife-dir decompiled/v_heavy_knife
# -> decompiled/v_heavy_grenade (QC + SMD + BMP), then merge-zhands as usual
```

| Option | Meaning |
|---|---|
| `--knife-dir DIR` | decompiled zombie knife model (the hands) |
| `--out DIR` | output (default: next to it, `_knife` → `_grenade`) |
| `--donor DIR` | another grenade model to take the grenade + motion from |
| `--modelname NAME` | `$modelname` (default `<out name>.mdl`) |
| `--snug-max-deg`, `--curl`, `--grip-offset`, `--weapon-offset` | as in `retarget` |

Studio: right-click a `zhands` asset ▸ **Retarget…** ▸ *Make grenade (zombie
hands)* (the default mode for zombie hands) → `v_<zombie>_grenade`.

## Parts, oversized hands

Different zombies rarely share a rig, and claws rarely have the 4+ fingers
the hand matcher looks for — so the rigs are **pooled** (merge-v's bone pool):
only one zombie's hands draw at a time and every sequence is one zombie's, so
each rig's bones land on shared slots by structure (parent under parent,
largest rig first), reparents solved exactly per frame; the grenade's `gren_*`
bones keep their names. A part closes before 127 bones, 30 models (hands
entries + the grenade's 2 under 32 submodels) or a sequence over 64K (pooling
reshapes channels; measured with studiomdl's exact replica), a zombie's models
(knife, grenade, variants) always in one part, and the manifest names each
model's part (`model = …`). A model whose hands entry would exceed studiomdl's
2048 vertices / normals for one submodel ships as it is (`standalone/`).

## In the Studio (zombie claws)

Zombie hands assets need no particular names in a `merge-zhands` build: one
not named `v_<zombie>_knife` / `_grenade` (`v_smoker`, `alien_claw`) is staged
under such a name (`v_smoker_knife`) and the manifest keeps the asset's name.
Claws without a grenade get the shared frog grenade: the build makes one with
**Make grenade** (the bundled banshee donor) and keeps it as an asset, so the
next run reuses it; claws whose hands the retarget cannot find (rigs without
finger bones) are merged without one (a warning says which). Models already
holding a bomb (`grenade`, `bomb`, `nade` in the name) get none.

On a live server's 19 zombie claws and 4 zombie bombs (37 models with the 13
grenades made): 3 parts (were 10 before pooling) + 1 model on its own (hands
over 2048 vertices), all compiled with stock studiomdl, the pose gate passing
on every part.
