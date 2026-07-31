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
| §7.5 | Phase 3 — weapon placement | ✅ done (zero-offset) |
| §7.6 | Phase 4 — grip solve (finger curl) | 🟡 core done |
| §7.7 | Phase 5 — unify skeleton + export | ✅ done |
| §7.8 | Phase 6 — post-export text verify | ✅ done |
| §8 | full per-frame metrics/report | 🟡 partial |

Working end-to-end in headless Blender, **all phases 0–6**: the reference hands
follow the animation, curl around both grips, the skeleton is unified, and the
model is exported and text-verified. `retarget … --out <dir>` now writes a full
compilable model (merged mesh SMD + 16 anim SMDs + regenerated QC) that passes
the Phase 6 gate (8/8 checks). Remaining refinements: **Phase 4 numeric
validators (BVH)** and the §8 per-frame metric CSV. See `stages/phase34.md` and
`stages/phase56.md`.

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
3. **Unmatched target finger joints are held, not redistributed** — §7.3.6 asks
   for the source terminal joint's rotation to be distributed across extra target
   joints proportionally to bone length; instead any target joint deeper than the
   source chain keeps an identity basis (held). The hold is decided by chain
   *depth*, not by the `*Nub` name. Fine for v_elite (only Nub tips are unmatched);
   a real gap for a source with genuinely shorter fingers, to be closed if needed.
4. **Hand-set closure is broader than the spec** — §5 closes the weighted set over
   bones on paths *between* weighted bones; the implementation adds *all* non-weapon
   ancestors up to the root. Equivalent for these rigs (the extra bones are the arm
   root/forearm) and the disjointness assertion still guards correctness.
5. **Anchor fallback** — the per-arm translation anchor is the forearm bone hanging
   off the held root; if no such bone exists (a hierarchy deeper than
   forearm-off-root) it falls back to anchoring the wrist directly.
6. **Node order — reference bones not strictly first (§7.7/§2.1)** — BST writes the
   node table in `armature.data.bones` (hierarchy) order, so a gun bone parented
   under a wrist nests inside that hand's subtree rather than trailing all
   reference bones. The load-bearing invariants still hold: mesh and every anim
   share one identical table, and reference bone names/parents/rest are unchanged.
   Reported as a warning by the Phase 6 gate, not a failure.
7. **Reference mesh preserved by position, not byte-identical (§2.1)** — a Blender
   round-trip recomputes vertex normals and may reorder triangles, so byte
   identity is impossible. Phase 6 instead proves every reference vertex position
   survives within tolerance (measured 0.00000 u), which is the reshape-relevant
   invariant.
8. **Rotation continuity checked geodesically (§7.8)** — after the text-level Euler
   unwrap, the gate checks the per-frame *geodesic* rotation delta against the
   threshold rather than raw Euler-component jumps, so fast-but-smooth motion (a
   finger snapping in a 30 fps reload) is accepted while a real teleport still
   fails.

## Done — Phase 3 & 4 (see stages/phase34.md)
- **Phase 3 (§7.5)** — zero weapon offset (default): the weapon stays where its
  own animation puts it and the hands come to it. A non-zero offset is explicitly
  rejected (not implemented) rather than silently ignored.
- **Phase 4 core (§7.6)** — per-finger CCD (`grip_ik.py`, pure Python) curls each
  reference finger so its tip lands on the *original* finger's tip, absorbing the
  extra reference length as wrap. Joint-limited, warm-started from the previous
  frame. Tip error ~0.0005u across all v_elite sequences; fingers wrap the grips
  (verify renders regenerated).

## Done — Phase 5 & 6 (see stages/phase56.md)
- **Phase 5 (§7.7)** — `retarget/unify.py` (pure) finds each gun subtree and maps
  it to the correct reference wrist; the worker appends the weapon bones into the
  reference armature (names/offsets preserved), rebinds the weapon mesh, transfers
  the weapon animation per frame, deletes the originals, and exports a merged mesh
  SMD + one anim SMD per sequence via BST. A regenerated QC (`retarget/qc_build.py`)
  makes the output compilable.
- **Phase 6 (§7.8)** — `retarget/verify_smd.py` (pure) parses the emitted text and
  proves the §2 constraints: identical node tables, reference bones/parents/rest
  preserved, hand translation frozen, no NaN/Inf, rotation continuity, frame
  indices, reference geometry preserved. `retarget/euler_unwrap.py` unwraps the
  Euler tracks in place first (BST re-derives Euler per frame, so continuity is
  enforced on the text, as the spec requires).

## Not started / remaining
- **Phase 4 validators** — the §7.6/§8 numeric accept/reject (BVH overlap band
  vs the original, `d_max` over-penetration guard) and the extra objective terms
  (distal-direction, temporal/base regularisation, priority relaxation). Only
  tip-error is measured today.
- **§8 metrics CSV** — `report.csv` flattened per-frame/per-finger summary.

## Commits
- Remove obsolete pure-Python mechanisms
- Scaffold pipeline: config, driver, worker Phase 0-1
- Phase 2a: geometric bone correspondence
- Phase 2b: rotation retargeting by direct matrix keying
- Orient retargeted hands to the source's absolute pose, not the T-pose
- Pair arms by side, not palm-normal, so L/R is correct by default
- Add Phase 3 (zero-offset) and Phase 4 core grip solve
- Phase 5: unify skeleton, transfer weapon anim, export merged SMDs + QC
- Phase 6: text-verify emitted SMDs; Euler-unwrap animation tracks in place
9. **FAIL still leaves outputs on disk (§8.3)** — the spec says a FAIL sequence
   writes no output; the pipeline instead keeps the written SMDs/QC when the
   Phase 6 gate fails and signals failure via exit code 2. Deliberate: the
   emitted files are the primary debugging artifact for a text-level gate
   failure. Nothing downstream consumes the output directory automatically.
10. **Euler component continuity is a hard invariant, not a threshold (§7.8)** —
   alongside the geodesic check (deviation 8), the gate now fails any per-frame
   Euler *component* jump above π: after a correct unwrap every component is
   within π of the previous frame, so a larger jump can only mean the unwrap was
   skipped or broken.
