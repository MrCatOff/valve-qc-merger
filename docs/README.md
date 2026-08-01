# valve-qc-merger wiki

Project documentation lives here, one page per topic.

- [`retarget`](retarget.md) — rebuild a GoldSource viewmodel on the reference
  hands: usage, discovery rules, output contract, pipeline overview,
  verification gate, troubleshooting.
- [`merge-view`](merge-view.md) — merge a folder of decompiled view-models
  into combined models with canonical hand skeletons and per-weapon
  bodygroups.
- [`merge-player`](merge-player.md) — merge decompiled p_ (player-held) weapon
  models: shared Bip01 chain, one bone per held object.
- [`merge-world`](merge-world.md) — merge decompiled w_ (dropped-weapon)
  models onto a single hitboxed weapon bone.
- Full example config with every attribute:
  [`configs/example_retarget.toml`](../configs/example_retarget.toml).
