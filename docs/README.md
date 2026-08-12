# valve-qc-merger wiki

Project documentation lives here, one page per topic.

- [`retarget`](retarget.md) — rebuild a GoldSource viewmodel on the reference
  hands: usage, discovery rules, output contract, pipeline overview,
  verification gate, troubleshooting.
- [`merge-v`](merge-v.md) — merge a folder of decompiled view-models
  into combined models with canonical hand skeletons and per-weapon
  bodygroups.
- [`merge-p`](merge-p.md) — merge decompiled p_ (player-held) weapon
  models: shared Bip01 chain, one bone per held object.
- [`merge-players`](merge-players.md) — merge CSO player-character body models
  onto one canonical CS 1.6 rig: a `skin` bodygroup of many bodies sharing the
  donor's animations (unneeded slots voided with a placeholder).
- [`merge-w`](merge-w.md) — merge decompiled w_ (dropped-weapon)
  models onto a single hitboxed weapon bone.
- [Building the `tmp/wpn_unpacked` pack](wpn_unpacked-build.md) — a worked
  end-to-end record: grouping a mixed CSO dump by `p_`/`w_`/`v_`, the merge
  commands + exclusions, and the compile steps (LF + backslash fixups) that
  produced 10 stock-compilable models.
- Full example config with every attribute:
  [`configs/example_retarget.toml`](../configs/example_retarget.toml).
