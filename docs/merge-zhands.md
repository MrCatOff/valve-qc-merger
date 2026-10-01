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
  v_zhands.qc      $bodygroup "hands" { ... } + $bodygroup "grenade" { blank, grenade }
  hands/<entry>.smd   one per distinct hand mesh
  grenade/grenade.smd the shared grenade
  v_<zombie>_<role>/  that model's sequences
  *.bmp, models.ini
```

`pev_body = hands_index + n_hands * grenade_on`. `models.ini` gives each
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
