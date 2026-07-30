# Progress

Status of the Blender-driven retargeting pipeline against
`TECHNICAL_SPECIFICATIONS_V2.md`. Updated as phases land.

## Summary

| Spec | Phase | Status |
|---|---|---|
| §7.1 | Phase 0 — scene setup | ✅ done |
| §7.2 | Phase 1 — import & consolidate | ✅ done |
| §5 | runtime assertions | 🟡 partial |
| §7.3 | Phase 2a — bone correspondence | ✅ done |
| §7.4 | Phase 2b — rotation retarget | ✅ done (core) |
| §6 | process model | 🟡 partial |
| §9 | CLI | 🟡 partial |
| §10 | testing | 🟡 partial |
| §7.5 | Phase 3 — weapon placement | ⬜ not started |
| §7.6 | Phase 4 — grip solve (finger curl) | ⬜ not started |
| §7.7 | Phase 5 — unify skeleton + export | ⬜ not started |
| §7.8 | Phase 6 — post-export text verify | ⬜ not started |
| §8 | full per-frame metrics/report | 🟡 partial |

Working end-to-end and rendered in headless Blender: **Phases 0, 1, 2a, 2b**.
Remaining: **Phases 3, 4, 5, 6** (Phase 4 finger curl is the substantive next step).

## Done

### Phase 0 — scene setup (§7.1)
- Enable Blender Source Tools headless; assert its import operator registered.
- Clean scene, `frame_start = 0`, FPS from config, unit-scale asserted at 1.0.

### Phase 1 — import & consolidate (§7.2)
- Import reference hands (`Bip01`), weapon `-PV` mesh (`BoneNN`), original hand
  mesh, and one animation SMD.
- Animation attaches to the weapon rig (identical bones) via BST `APPEND`; other
  rigs imported with `NEW_ARMATURE` to keep them separate.
- Weapon mesh and original-hand mesh bound to the single animated **source** rig;
  reference mesh stays on the immutable **reference** rig.

### §5 assertions (partial)
- Done: identical node tables across weapon/original-hands/anims (driver, text);
  hand vs weapon bones disjoint by mesh weight; hand-set closure so unweighted
  wrists are included; arm count discovered (≥1).
- Not yet: gun-subtree→arm bijection check.

### Phase 2a — bone correspondence (§7.3)
- Pure-Python, `bpy`-free (`retarget/correspondence.py`), unit-tested without
  Blender on synthetic rigs.
- Discover arms (wrist = bone with ≥4 finger children); identify thumb by
  abduction (planar-outlier cross-check); build an intrinsic hand frame
  (palm-forward, knuckle, palm-normal); map wrist, forearm and every finger joint
  proximal-to-distal; hold reference `*Nub` tips where the source is shorter.
- **Arm pairing** matches arms by side (wrist direction from the arms' centroid).
  Fully decisive on v_elite (score 2.0, margin 4.0) → `L→Bone26`, `R→Bone03`.
  `swap_arms` config override remains for arms that are not spatially separated.

### Phase 2b — rotation retarget (§7.4)
- Pure-Python math (`retarget/pose_retarget.py`): compute each target bone's
  `matrix_basis` directly (no constraints, no visual-keying bake). Held tips keep
  identity basis; one anchor per arm carries translation, solved in closed form so
  the wrist lands on the source wrist.
- **Anatomical absolute-orientation**: because the reference hands are a flat
  T-pose whose bind orientation is unrelated to the grip, the retarget re-expresses
  the source's motion in each hand's anatomical frame so the hand adopts the
  source's absolute orientation (arm reaches along the grip toward the weapon).
- Worker keys `location` + `rotation_euler` across the whole sequence.

### Supporting
- `retarget/config.py` — full config incl. §11 author decisions as defaults;
  TOML + dict loaders.
- `retarget/driver.py` — non-`bpy` orchestrator: locate Blender, resolve inputs,
  §5 text gate, one headless worker per sequence, collect JSON reports.
- `retarget` CLI command wired into the registry.
- Old pure-Python mechanisms removed; SMD/QC I/O kept for Phase 6.
- 40 tests pass; ruff and mypy --strict clean.

## Deviations from the spec (deliberate)
1. **Arm pairing** — side-matching instead of the spec's palm-normal (§7.3.3): a
   flat T-pose reference has no chirality and shares no orientation with the grip
   pose, so palm-normal had no signal and paired L/R backwards.
2. **Hand orientation** — added anatomical absolute-orientation in Phase 2b: the
   spec named the rest-pose divergence risk (§7.4) but left the fix open; without
   it the T-pose bleeds through and the forearms splay along X.

## Not started
- Phase 3 — weapon placement (default zero offset; hands come to the weapon).
- Phase 4 — grip solve: curl each longer finger onto the weapon surface to match
  the original hand's contact (not zero-overlap). **Next.**
- Phase 5 — append weapon subtree to the reference armature, rebind, transfer
  weapon animation, delete originals, export SMDs via BST.
- Phase 6 — parse emitted SMDs and prove the §2 constraints at text level.
- §8 full per-frame/per-finger metrics in `report.json`.

## Commits
- Remove obsolete pure-Python mechanisms
- Scaffold pipeline: config, driver, worker Phase 0-1
- Phase 2a: geometric bone correspondence
- Phase 2b: rotation retargeting by direct matrix keying
- Orient retargeted hands to the source's absolute pose, not the T-pose
- Pair arms by side, not palm-normal, so L/R is correct by default
