# Fixes 3 — grip quality: natural finger curl + weapon offset (§7.5/§7.6/§11.2)

User report: fingers bent into anatomically impossible positions and the thumb
turned the wrong way; the original hands are smaller, so the weapon likely needs
a Y shift. Root-caused and fixed in two parts.

## Diagnosis

Zoomed grip renders (ours vs original, four views — see
`tmp/verify/model/zoom_R.png`) showed:

1. **Knuckle overshoot.** The reference palm (wrist→knuckle centroid) is
   **0.83–0.92 u longer** than the original's. The wrist is anchored on the
   source wrist, so the reference knuckles land past the grip front where the
   original's knuckles sat behind it; the fingers then had to curl backward and
   sideways to reach the source fingertip targets.
2. **Unconstrained CCD.** The Phase 4 solver applied free 3-DOF swings clamped
   at ±150° per Euler axis — enough freedom to fold joints out-of-plane and
   backward (the claw), and to curl the thumb along the wrong arc.

## Fix 1 — hinge-constrained finger solve (grip_ik.py rewritten)

Anatomy is now structural instead of numerical:

- Every joint of a finger rotates about **one fixed world hinge axis** — the
  hand's posed knuckle line (recomputed per frame, oriented away from the thumb)
  for the four fingers; the base→target arc-plane normal for the thumb (planar
  curl straight toward its own contact point — it cannot fold the wrong way).
- Scalar flexion limits per joint depth replace the per-axis Euler boxes:
  `hinge_mcp (-25°..100°)`, `hinge_pip (0°..110°)`, `hinge_dip (0°..90°)`,
  `hinge_thumb (-60°..90°)` (config).
- The hinge's curl direction is **self-calibrated** per finger (probe ±axis,
  keep the sign that approaches the target) — no handedness convention can flip
  a finger, which is what had turned the thumb the other way.
- Warm start is now the previous frame's hinge angles; the thumb chain is
  identified per wrist by the §7.3.4 abduction rule.

Deviation from §7.6's letter (damped least squares over 3 rotational DOFs with
per-axis limits) recorded in progress.md: a hinge is strictly less expressive,
and that is the point — the removed DOFs are exactly the anatomically invalid
ones. Tip-position targeting, warm start and relaxation-by-limits survive.

## Fix 2 — weapon offset implemented (§7.5/§11.2)

`weapon_offset` (previously rejected as unimplemented) is now the sanctioned
single constant rigid translation per weapon:

- Applied identically to the gun bones (`key_guns`) and the Phase 4 fingertip
  targets, so grip contact is preserved relative to the shifted weapon; hands
  and wrists stay put.
- Recorded in every worker report; the Phase 6 `weapon_pose_matches_source`
  check compensates for exactly the configured offset (unit-tested both ways).
- **v_elite value: `[0, -0.6, 0]`** in `configs/v_elite.toml`, derived from the
  palm-length mismatch (palm-forward is world −Y at idle: −0.998/−0.999 both
  hands) and calibrated visually across candidates 0/−0.6/−0.9/−1.2 (§8.4
  one-time calibration; grid in the session scratchpad): −0.6 reproduces the
  original's wrap depth; more looks claw-like, less leaves the hand chasing the
  gun.

## Fix 3 — temporal continuity of the curl (found by the hardened gate)

The first full run FAILED its own Phase 6 gate (exit 2): the left thumb's
distal joint popped 149° between draw frames 13→14. Two causes, two guards:

- The thumb's arc-plane axis is recomputed per frame; near-collinear frames can
  flip its hemisphere. The axis is now **hemisphere-aligned to the previous
  frame's** (and degenerate frames reuse it outright).
- Even with a stable axis, the target's projection onto the hinge plane can
  legitimately reverse when the target crosses the hinge line — CCD then wants
  a ~π jump in one frame. Added §7.6's temporal regularisation in hinge form:
  `solver.max_step_degrees` (default 35°/frame) caps each joint's per-frame
  travel from its warm start, spreading such corrections over a few frames.

## Verification

- Gates: 69 pytest passed (6 rewritten/new hinge-IK tests + offset-compensation
  test), ruff clean, mypy --strict clean.
- Full 16-sequence run with `--config configs/v_elite.toml`: all EXPORTED,
  **verify PASS (11/11), exit 0**; `tmp/verify/model/` regenerated. (The
  interim thumb-pop run correctly exited 2 — the euler-continuity gate caught a
  solver defect in practice, which is what it is for.)
- Weapon placement proven from the emitted text: every gun bone's world pose ==
  source + (0, −0.6, 0) within 0.0002 u / 0.001° across sampled frames of
  idle/draw/reload/shoot_right1.
- Grip residuals (tip error vs the shifted contact targets): mean ≈ 0.56 u in
  the steady grip, larger transients in draw/reload — the accepted cost of
  forbidding anatomically invalid reaches; the visual grip is the acceptance
  criterion per §8.4.
- Visual: `tmp/verify/model/zoom_R.png` / `zoom_L.png` (grip close-ups, ours vs
  original) and `compare_idle_f4.png` (full model) — fingers wrap the grip in
  one even plane like the original, index at the trigger guard, thumb crossing
  the grip side (no longer inverted), no backward joints, both hands.

## Fix 4 — finger spacing (user feedback: fingers spread, original pressed tight)

The hinge solver rebuilds each finger from the reference hand's REST pose — a
relaxed, splayed fan — so the original grip's tight finger adduction never
transferred and the fingers wrapped the grip with visible gaps
(reference ideal: `tmp/images/fixed.png`).

Fix: **abduction aim**. Per finger per frame, the base segment is re-aimed at
the source finger's posed segment direction by a minimal rotation folded into
the MCP as a `pre_basis` (the hinge then curls on top, its axis riding the
aimed seat). World directions are directly comparable because the wrist is
pinned to the source wrist and the hand adopts the source's absolute
orientation. Pitfall found on the way: BST-imported bones have **synthetic
tails** (SMD stores none), so the direction must be head-to-child-head, not
`pose_bone.tail` — the first attempt aimed at garbage and tripled the tip
error before the corrected version *reduced* it below the no-aim baseline
(idle mean 0.56 → **0.22 u**, and the fingers sit pressed together like the
original's).

Verified: 70 pytest (new `pre_basis` re-aim test), ruff + mypy --strict clean,
full run PASS (11/11) exit 0, grip renders regenerated — both hands now match
the original's finger spacing (`zoom_R.png` / `zoom_L.png`).

## Fix 5 — thumb wrap via hand_offset (user feedback + constraint clarification)

The thumb still did not wrap the grip like the original. Cause: `weapon_offset`
shifts the grip-contact targets with the gun, and the −0.6 Y shift pulled the
thumb's target forward along the slide — the thumb reached it in an extended
pose instead of hooking the true original contact point.

Per the user's direction (hands may move along XYZ; nodes/triangles must stay
untouched for future bodygroup merging), the shift moved from the weapon to the
hands: **`hand_offset`** anchors every wrist at `source wrist + offset` while
the weapon — and the contact points on it — stay exactly where the animation
puts them. Only the anchor bone's pose translation changes; node table,
triangles and every other bone's local translation are untouched (the §2
invariants the gate proves are unaffected). v_elite: `hand_offset = [0, 0.6, 0]`
replaces `weapon_offset = [0, -0.6, 0]` — same relative seating, but:

- the **weapon is back at its authored zero-offset position** (§7.5 default
  restored: attachments, dual-gun sync, screen framing exact — verified from
  the emitted text at 0.00004 u / 0.001°), and
- the **thumb curls onto the true original contact and wraps the grip** (Z
  candidates 0/−0.3/+0.3 rendered; 0 matches the original's grip coverage).

Verified: 71 pytest (new anchor-offset test), ruff + mypy --strict clean, full
run PASS (11/11) exit 0, idle tip error mean 0.216 u; final renders in
`tmp/verify/model/zoom_{R,L}.png`.
