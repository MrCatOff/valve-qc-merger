# merge-props

Effects, projectiles, props, NPC-like models — anything that is neither a
weapon nor a player — merged into a few models. `pev_body` picks the model;
each keeps its own bones, animations, textures and attachments. The same
merge takes view models whose hands ARE the model (zombie claws) or that have
no hands (see merge-v's no-hands part).

```bash
valve-qc-merger merge-props DECOMPILED_DIR --out OUT [--name props]
```

In the Studio: importing a server folder brings such models in as **Props &
effects** assets (they used to be left out); a `merge-props` build merges
them.

## How

- **No hand canonicalisation.** Bones are shared by name; the first model
  that has a bone fixes its parent, a model that hangs it elsewhere is
  re-solved onto it frame by frame (world poses unchanged). Models built on
  one rig share their bones.
- **One submodel per model** in the `weapon` group: every bodygroup's first
  entry, packed under studiomdl's 2048 vertices / 2048 normals; what does not
  fit goes to `weapon_2`… (blank for the other models). Alternatives inside a
  bodygroup are dropped (a warning says which).
- **Sequences** keep their labels (`<model>__<label>` on a clash); the
  manifest lists each model's sequences in its own order (`anim_<label> =
  <merged number>`).
- **Textures** are deduped by content, renamed on a clash, render modes kept.
- **Attachments** (4 at most in GoldSource) become shared slot bones every
  model's sequences drive to its own points.
- **Repairs** so stock studiomdl compiles what the originals' compiler took:
  a mesh without a texture gets a grey placeholder; a model without any mesh
  (`muzzle_*.mdl`: bones, an attachment, a hitbox) gets an invisible
  zero-area triangle on each bone an attachment / hitbox names (studiomdl
  drops vertex-less bones).

## Parts

A part closes at 32 submodels per group, 127 bones (+ attachment slots), the
texture budget (80), 255 sequences and a `pev_body` range of 256 (the engine
sends `body` in 8 bits). A model too big on its own ships as it is
(`standalone/<name>/`, `standalone = 1` in the manifest).

On a live server's 50 effect / prop models: 5 merged models, all compiled
with stock studiomdl, every model's first frame within 0.05 u of its original.
