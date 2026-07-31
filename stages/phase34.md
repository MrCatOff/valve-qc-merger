# Phase 3 & 4 report

Input for the next verification pass. Gates green: **48 pytest passed, ruff
clean, mypy --strict clean** (34 files). Verify renders regenerated in
`tmp/verify/`.

## Phase 3 — weapon placement (§7.5)

Zero offset, as recommended: the weapon is never moved; it stays where its own
animation puts it and the hands come to it. This preserves the authored recoil /
sway / reload arcs, keeps attachment points aligned, and keeps the two guns in
sync. A configured non-zero `weapon_offset` is **rejected** with a clear error
(`worker.run`) rather than silently ignored — the constant-rigid-offset path
(§7.5) is not implemented yet because nothing needs it.

## Phase 4 — grip solve, core (§7.6)

**Implemented** (`retarget/grip_ik.py`, pure Python, unit-tested without Blender):

- **Target = the original fingertip.** For each finger, per frame, the tip target
  is the world tail of the deepest *source* finger bone (where the original,
  shorter finger ended — i.e. on the grip). Curling the longer reference finger to
  reach that nearer point wraps the extra length around the grip instead of poking
  through it, exactly per §7.6.
- **Solver = Cyclic Coordinate Descent** over the finger chain's DOF joints,
  expressed in each joint's local `matrix_basis` so it composes with Phase 2b.
  Distal-to-proximal swings, joint-limited, warm-started from the previous frame
  for temporal coherence. Held `*Nub` tips are not DOF.
- **Integration** (`worker.retarget`): after the rotation retarget produces the
  open-hand pose, the wrist world pose is fixed and each finger is solved and its
  bases overwritten before keyframing.

**Solver parameters used** (config defaults):

| Param | Value |
|---|---|
| iterations (`max_iterations`) | 40 |
| joint limits (MCP/PIP/DIP) | ±150° per axis (generous) |
| warm start | previous frame's solution |
| tolerance | 1e-3 |

Note on limits: the spec's tight flexion-dominant limits assume a known local
flexion axis, which differs per rig; with the wrong axis they blocked the curl
(tip error ~2.7u). Because the target is the original contact point, the natural
curl to reach it is already plausible, so generous limits are the safe default
and produce a correct-looking grip. Proper per-rig flexion limits are a §8.4
calibration.

**Per-sequence metrics** (tip error, world units; the only validator built so far):

| Sequence | Frames | tip_err mean | tip_err max |
|---|---|---|---|
| idle | 9 | 0.0004 | 0.0009 |
| draw | 33 | 0.0006 | 0.0019 |
| reload | 138 | 0.0005 | 0.0026 |
| shoot_right1 | 41 | 0.0005 | 0.0009 |

Every fingertip reaches its target to < 0.003u. No relaxation was needed (targets
are within reach), so the priority order is untested in practice.

## Deliberately deferred (Phase 4 remainder)

- **BVH validators (§7.6/§8):** overlap band (`overlap_ref ∈ [0.5,2.0]·overlap_src`,
  zero = fail) and the `d_max` unsigned over-penetration guard vs the original
  hand. These need Blender `BVHTree` and are the numeric accept/reject the spec
  wants; today the grip is validated by tip error + visual inspection only.
- **Extra objective terms:** distal-direction (`w_d`), temporal (`λ_t`) and
  base-pose (`λ_b`) regularisation. Warm-starting gives coherence in practice
  (metrics are stable frame-to-frame), but the smoothing terms are not in.
- **Priority relaxation** (pinky→thumb): coded target is always reachable here, so
  no relaxation path exercised.
- **`e_pos` normalisation** by distal phalanx length (§7.6) — raw tip error for now.

## New tests
- `tests/test_grip_ik.py`: reachability, curl-reduces-distance, joint-limit
  respect, warm-start acceptance (4 tests).

## Renders to check
- `tmp/verify/<seq>/ours_*_f<N>.png` and `tmp/verify/closeup/ours_side.png` —
  fingers now curl into the trigger guard and wrap the grip, matching the original
  (`tmp/verify/closeup/orig_side.png`).
