# Technical Specification v2: Automated Weapon Animation Retargeting CLI

**Supersedes:** `TECHNICAL_SPECIFICATIONS.md` (v1).
**Companion:** `SPEC_REVIEW.md` — the findings that motivated each change.
**Status:** implementable, pending the five decisions in §11.

---

## 1. Objective

A headless Python CLI, driven by Blender (`bpy`) and the Blender Source Tools (BST)
add-on, that retargets a weapon's animation set from its original hands onto a fixed
reference hand skeleton, resolving grip geometry numerically against a ground-truthed
criterion rather than by eye.

---

## 2. Non-negotiable constraints

1. **Immutable reference hands.** No reference bone is renamed, reparented, added or
   removed; no reference bone rest offset is altered; the hand mesh is never scaled,
   reshaped or re-bound. Weapon bones **are** appended to the reference hierarchy in
   Phase 4 — this is the one permitted extension and is verified in Phase 5.
2. **Animation-only output.** The only values that change per frame are bone rotations,
   plus translation on a single designated anchor bone per arm (§7.4). Every other hand
   bone's local translation equals its rest offset in every exported frame; Phase 5 proves
   this from the exported text.
3. **No scale, anywhere.** No constraint, matrix or bake may introduce scale onto the
   reference skeleton.
4. **No visual guessing in the loop.** The solver's accept/reject decision is numeric.
   Rendered frames may be produced as diagnostics only, and one-time visual calibration of
   tolerances (§8.4) is permitted and must be recorded in the config.

---

## 3. Conventions

| Concept | Convention |
|---|---|
| Coordinate system | Source/SMD: Z-up, X-forward, right-handed. Never apply object-level transforms to imported armatures. |
| Import/export scale | Exactly 1.0, asserted on both ends. |
| Rotations in SMD | Euler XYZ, radians, parent-relative. |
| Frame indexing | SMD `time` starts at 0. Blender `frame_start` is set to 0 to match; any off-by-one is a Phase 5 failure. |
| FPS | Taken from config, must match the QC `$sequence ... fps`. Recorded in the report. |
| `src` / `tgt` | `src` = original `BoneNN` rig; `tgt` = reference `Bip01` rig. |
| World matrices | `M_*` denotes a 4×4; `R_*` its rotation part. |

**Spaces.** All correspondence, retargeting and grip targets are computed in **weapon-local
space** — the frame of the weapon bone that carries the gun mesh for that arm. This makes
the pipeline invariant to where the weapon sits in the world and makes any future weapon
offset (§7.5) a no-op re-derivation rather than a re-solve.

---

## 4. Inputs and outputs

**Inputs**

| Path | Role |
|---|---|
| `tmp/hands/reference_hands.smd` | Immutable reference hands: `Bip01` hierarchy + hand mesh. |
| `tmp/pistols/view/v_elite/v_elite-PV.smd` | Weapon mesh + full `BoneNN` rig (reference pose). |
| `tmp/pistols/view/v_elite/f_elite_Male_hand_Low.smd` | Original hand mesh — the contact ground truth. |
| `tmp/pistols/view/v_elite/v_elite_anims/*.smd` | Animation sequences. |

**Outputs**

| Path | Content |
|---|---|
| `<out>/<weapon>-PV.smd` | Reference hands + weapon mesh on the unified skeleton. |
| `<out>/anims/<seq>.smd` | One retargeted sequence SMD per input, identical node table. |
| `<out>/report.json` | Per-sequence, per-frame, per-finger metrics and status (§8). |
| `<out>/report.csv` | Flattened summary for diffing between runs. |
| `<out>/diag/<seq>/<frame>.png` | Optional diagnostic renders. Never an acceptance gate. |

---

## 5. Facts about the data — and the assertions that enforce them

v1 listed these as verified. v2 re-verifies each at runtime and **fails loudly** on
violation, because every downstream phase depends on them.

| Fact | Runtime assertion |
|---|---|
| One unified `Bone01…Bone65` rig across weapon reference, original hands and all animations. | Node tables of all input SMDs for a weapon are identical (names + parents). |
| No name correspondence between `Bip01 *` and `BoneNN`. | Not asserted; correspondence is built geometrically (§7.3). |
| Hand bones and weapon bones are disjoint by mesh weights. | No bone carries weight ≥ `w_min` (default 0.05) from **both** the original hand mesh and the weapon mesh. Failure aborts with the offending bone list. |
| Weapons may be multi-arm. | Arm count `N` is discovered, never assumed. `N ≥ 1`; each arm has exactly one wrist and 4–5 finger chains. |
| Each gun subtree hangs off its arm's chain. | Every weapon-mesh-weighted bone has a hand bone among its ancestors, and the mapping arm→gun is a bijection. |

**Hand-set closure.** The hand bone set is the weighted set closed under connectivity: any
bone lying on a path between two weighted hand bones joins the set even at zero weight
(wrists frequently carry no weights).

---

## 6. Environment, determinism and process model

- **Pinned:** one Blender version (4.2 LTS recommended) and one BST version, both recorded
  in `report.json`. `bpy` has drifted materially across 3.6 → 4.x (bone collections
  replacing layers, operator argument changes); do not assume signatures — verify against
  the pinned build.
- **Launch:** `blender --background --factory-startup --python retarget_worker.py -- <args>`.
  `--factory-startup` guarantees a clean state and discards saved add-on preferences, so
  BST is enabled explicitly in-script via `addon_utils.enable("io_scene_valvesource")`.
- **One subprocess per sequence.** A thin non-`bpy` driver spawns workers. This gives
  memory isolation (no orphan-data accumulation across 30 sequences), crash containment,
  and parallelism via `--jobs`.
- **Determinism.** All ordering is by explicit sort key, all tie-breaks are deterministic,
  the IK solver is seeded from config. Two runs on identical inputs must produce
  byte-identical SMDs.
- **Prefer data over operators.** Where a result can be computed and keyframed directly,
  do that instead of calling a `bpy.ops` operator. Operators depend on context, selection
  and UI areas — several are unavailable or behave differently in `--background`.

---

## 7. Pipeline

### 7.1 Phase 0 — Scene setup

Empty scene, `frame_start = 0`, FPS from config, unit scale 1.0, BST enabled and its
import/export scale asserted at 1.0.

### 7.2 Phase 1 — Import

Import `reference_hands.smd`, `<weapon>-PV.smd`, `f_elite_Male_hand_Low.smd`, and the
target animation SMD. BST creates a separate armature per SMD unless directed otherwise;
after import, assert exactly the expected number of armatures and bind each mesh to its
intended one. Record every armature's rest matrices before anything is posed.

### 7.3 Phase 2a — Rig discovery and bone correspondence

Names do not correspond, so correspondence is built from structure first and geometry
second. Structure is far more robust than position across two rigs in different poses.

1. **Classify.** Partition `BoneNN` into hand bones and weapon bones per §5.
2. **Discover arms.** Each arm is a connected subtree of the hand set. Its root is the
   most proximal hand bone; its wrist is the last bone before the branch into finger
   chains. Extract each arm's *signature*: number of chains, joint count per chain,
   relative chain-base positions.
3. **Pair arms.** Match reference arms to original arms by signature, then by Kabsch fit
   of the chain-base point sets in a wrist-relative normalised frame, scored by residual.
   Handedness is derived from `sign(dot(palm_normal × forearm_axis, chain_spread_axis))` —
   **never** from a world-X sign, which is meaningless for viewmodel rigs where both arms
   can sit on the same side of the origin.
4. **Identify the thumb.** Fit a plane through the four non-thumb chain bases; the thumb is
   the chain whose base is most displaced from that plane and whose first segment is most
   abducted from the mean chain direction. Cross-check against joint count. If the two
   signals disagree, abort with a diagnostic rather than guessing.
5. **Order the remaining four** by projection onto the knuckle-line principal axis, with
   index/pinky direction resolved by handedness from step 3.
6. **Match joint levels** proximal-to-distal within each chain.
   - Equal joint counts → one-to-one.
   - Source has fewer joints → the source terminal joint's rotation is distributed across
     the unmatched target joints proportionally to bone length; the residual is absorbed
     by the IK stage (§7.6).
   - `*Nub` tips have no source counterpart and hold their **rest-pose local transform**
     (identity local delta) in every frame.
7. **Report** the full map, per-pair fit residuals, and the rest-pose divergence measure
   from §7.4 — this is the single most useful artefact for debugging a bad result.

**Mirroring check.** If any arm's bone matrices have negative determinant, the rig contains
a mirrored duplicate; handle explicitly or abort. Silent orientation inversion is otherwise
the result.

### 7.4 Phase 2b — Rotation retargeting

Constraints are **not** used. Pose matrices are computed and keyframed directly, which
removes constraint evaluation-order, space-conversion and visual-keying pitfalls, and makes
Phase 4's bake step unnecessary.

**Per-bone rest correction**, computed once from the rest poses:

```
C(b) = R_rest_src(b)⁻¹ · R_rest_tgt(b)
```

**Per frame, per bone:**

```
R_pose_tgt(b) = R_pose_src(b) · C(b)
```

This applies the source's world-space delta-from-rest to the target's rest orientation.
Rotation only — no scale, and no translation except on the anchor bone below.

**Writing the pose.** Compute the target's local basis top-down, parents before children:

```
M_pose(b) = M_pose(parent) · [M_rest(parent)⁻¹ · M_rest(b)] · M_basis(b)
⇒ M_basis(b) = [M_pose(parent) · M_rest(parent)⁻¹ · M_rest(b)]⁻¹ · M_desired(b)
```

Keyframe `M_basis` directly. This is exact and needs no depsgraph round-trip per bone.

**Anchor policy — wrist-anchored (recommended; see §11.1).** Per arm, exactly one bone
carries translation: the arm root. Its translation is solved so the reference **wrist
lands exactly on the original wrist position**, with the forearm's retargeted orientation.
The forearm's proximal end is therefore free to float. Rationale: the reference and
original forearms differ in length, so both ends cannot be pinned, and the grip depends on
the wrist end. In a viewmodel the proximal end is cut off at the screen edge.

**Frustum guard.** If the reference forearm is longer than the original, its cut end may
enter frame. Test the cut-plane vertices against the viewmodel camera frustum from config
each frame and flag any intrusion in the report.

**Rest-pose divergence.** `C(b)` silently absorbs any difference in what the two rest poses
depict (flat open hand vs pre-curled). Compute and report the angular divergence per bone
at startup; large values mean the retargeted base pose starts far from correct and the IK
stage is doing more work than it should.

**Weapon bones** receive no retargeting. They are animated by the imported source data and
carry the gun mesh verbatim.

### 7.5 Phase 3 — Weapon placement

**Default: zero offset.** The weapon stays exactly where its own animation puts it, and the
hands move to it. This preserves the authored recoil, sway and reload arcs, keeps
attachment points (muzzle, shell eject, tracer origin) aligned with the geometry, keeps
dual-wield guns in sync, and preserves the viewmodel's art-directed screen framing.

If §11.2 decides an offset is required, it is a **single constant rigid transform per
weapon** — not per frame, not per sequence. It is solved by minimising the aggregate seat
residual across all frames of all sequences, reported as an explicit translation and
rotation, and signed off by the author. A per-frame offset is forbidden: it re-authors the
weapon's motion and desyncs the two guns.

### 7.6 Phase 4 — Grip solve

The acceptance idea from v1 is retained and its metric is replaced with one that is
actually computable on an open-shell mesh.

**Ground truth.** The original hands already grip the weapon correctly. The only problem is
that the reference fingers are a different length. So the target is not "no intersection"
— a correct grip intersects heavily — but **the reference fingertip resting where the
original fingertip rested, relative to the weapon.**

**Objective (per arm, per finger, per frame).** Solve the finger chain's joint rotations to
minimise, in weapon-local space:

```
E = w_p ‖p_tip_tgt − p_tip_src‖²                 # tip position, w_p = 1.0
  + w_d (1 − cos∠(d_distal_tgt, d_distal_src))²  # distal direction, w_d ≈ 0.3
  + λ_t ‖θ_f − θ_{f−1}‖²                          # temporal regularisation
  + λ_b ‖θ_f − θ_base‖²                           # stay near the retargeted base pose
```

subject to per-joint limits from config (flexion-dominant; MCP ≈ −20°…90° with small
abduction, PIP/DIP ≈ 0°…100°).

- Solved by damped least squares (or CCD) over the chain's DOFs.
- **Warm-started from frame `f−1`**, which is what makes the result temporally coherent
  and cheap. Independent per-frame solves land in different local minima and buzz.
- The extra reference finger length is absorbed as additional curl — the finger wraps
  further around the same surface instead of poking through it. This is the intended
  behaviour and follows directly from targeting the tip.

**Priority under over-constraint.** If joint limits make a target unreachable, relax in
this order: pinky, ring, middle, index, thumb. Record which fingers were relaxed and by how
much. Never distort the hand to force a target.

**Validation (not the driver).** After the solve, per frame and per arm, build the
reference-hand and original-hand BVHs from the *evaluated* meshes and record:

- `overlap_ref`, `overlap_src` — triangle-pair counts against the weapon BVH. Accept when
  `overlap_ref ∈ [0.5·overlap_src, 2.0·overlap_src]`. Zero is a **failure**, not a pass:
  it means the hand has detached from the gun.
- `e_pos(i)` — tip distance error, normalised by the reference distal phalanx length so
  the tolerance is scale-independent. Accept when `e_pos ≤ τ_pos` (default 0.15).
- `d_max(i)` — for reference vertices whose triangles report an overlap, the maximum
  `bvh_weapon.find_nearest()` distance; compared against the same measure on the
  corresponding original finger. Accept when `d_max_ref ≤ d_max_src + τ_d`. This is the
  over-penetration guard and it uses **unsigned** distance only — no inside/outside test is
  performed anywhere, because it is unreliable on an open shell.

**Depsgraph discipline.** Every matrix read and every `BVHTree.FromObject(obj, depsgraph)`
after any pose change uses a freshly evaluated depsgraph. Stale reads produce plausible,
wrong numbers. This is a global rule, not a step.

**Optional smoothing.** A light low-pass may be applied to the *correction delta* only,
never to the retargeted base motion. Any smoothed frame is re-validated; if it fails, the
unsmoothed solution is kept.

### 7.7 Phase 5 — Skeleton unification and export

1. **Unify.** Append the weapon bone subtree into the reference armature, preserving
   `BoneNN` names, hierarchy and rest offsets, parented under the appropriate reference
   wrist per the arm→gun map. Re-bind the weapon mesh to the unified armature with weights
   carried over unchanged. Reference bones keep their original names, parents and order,
   and appear first in the node table.
2. **Transfer weapon animation** onto the appended bones as raw keyframes.
3. **Delete** the original armature and the original hand mesh — only after step 2, since
   the original armature drives the weapon until then.
4. **Export** the mesh SMD and every sequence SMD. BST's exporter is driven by scene and
   object `vs.*` properties rather than a filepath argument; verify the exact interface
   against the pinned BST version. Every animation SMD must carry a node table identical to
   the mesh SMD's.

### 7.8 Phase 6 — Post-export verification on the emitted text

The final gate parses the written SMDs and proves the constraints at format level. This
catches anything that leaked through the Blender layer.

| Check | Enforces |
|---|---|
| Node tables of mesh SMD and all animation SMDs are byte-identical; reference bone names, parents and order unchanged. | §2.1 |
| For every hand bone except the designated anchor, local translation in **every** frame equals its rest local translation within ε. | §2.2, §2.3 — a leaked scale or a stray `COPY_TRANSFORMS` would show up here as changed bone offsets. This is a direct, cheap proof that the hands were not reshaped. |
| No NaN or Inf in any value. | Solver sanity |
| Euler continuity: no bone's Euler components jump by more than a configured threshold between adjacent frames. Non-continuous frames are **unwrapped in place** by choosing the representation nearest the previous frame. | H1 — `bpy.ops.graph.euler_filter()` needs a Graph Editor context that does not exist in `--background`, so continuity is enforced here at the text level. |
| Frame count and `time` indices match the source sequence, starting at 0. | §3 |
| Reference mesh triangle block is byte-identical to the input reference mesh block. | §2.1 |

---

## 8. Metrics, acceptance and failure policy

### 8.1 Per-frame record

For every frame, arm and finger: `e_pos`, `d_max_ref`, `d_max_src`, `overlap_ref`,
`overlap_src`, IK iterations, converged flag, joints that hit limits, relaxation applied,
frustum-intrusion flag.

### 8.2 Sequence status

- **PASS** — every frame meets every criterion in §7.6.
- **DEGRADED** — some frames required relaxation but all stayed within the over-penetration
  guard and the contact band.
- **FAIL** — any frame violates the over-penetration guard, falls outside the contact band,
  or fails Phase 6.

### 8.3 Failure policy

Never emit a broken frame silently. In `--strict` (default) a FAIL sequence writes no
output and exits non-zero. With `--allow-degraded`, DEGRADED sequences are written and
flagged; FAIL still writes nothing. A non-converged frame falls back to the previous
frame's solution to preserve continuity, and is recorded as degraded.

### 8.4 Tolerance calibration

`τ_pos`, `τ_d` and the contact-band multipliers are calibrated **once**, by running the
pipeline on a reference sequence and comparing metric distributions against the original
hands' own baseline. The chosen values and the calibration run are recorded in the config.
This is the one sanctioned use of human visual judgement, and it happens outside the
solver loop — the loop itself remains numeric (§2.4).

---

## 9. CLI

```
retarget \
  --reference tmp/hands/reference_hands.smd \
  --weapon-dir tmp/pistols/view/v_elite \
  --anims 'v_elite_anims/*.smd' \
  --out out/v_elite_myhands \
  --config configs/v_elite.toml \
  [--sequences idle,shoot1] [--frames 0:40] \
  [--jobs 4] [--strict | --allow-degraded] \
  [--dry-run] [--diag-renders] \
  [--log-level INFO] [--seed 0] [--report out/report.json]
```

The worker is invoked as
`blender --background --factory-startup --python retarget_worker.py -- <args>`, parsing
`sys.argv` after `--`.

**Config file** carries the non-argument knobs: FPS, weight threshold `w_min`, joint limits,
solver weights (`w_p`, `w_d`, `λ_t`, `λ_b`), tolerances, damping, iteration caps, viewmodel
camera parameters, and the calibration record from §8.4.

**Exit codes:** `0` all PASS · `1` some DEGRADED (with `--allow-degraded`) · `2` one or more
FAIL · `3` rig discovery or correspondence failure · `4` environment/assertion failure
(§5, §6).

`--dry-run` performs import, discovery and correspondence, writes the map and metrics, and
exits without solving or exporting. It is the fastest way to check a new weapon's rig.

---

## 10. Testing

1. **Unit — correspondence.** Synthetic rigs generated programmatically: 1 and 2 arms,
   3-joint and 2-joint fingers, mirrored arms, rigs rotated and translated arbitrarily,
   rigs with both arms on the same side of the origin. Assert the recovered map exactly.
2. **Unit — retargeting math.** With `src` and `tgt` identical, retargeting must be the
   identity to floating-point tolerance. With `tgt` a rotated copy, the result must equal
   the rotated source.
3. **Property — Phase 6 invariants.** Random perturbation of solver outputs must be caught
   by the §7.8 checks.
4. **Golden regression.** Metric distributions per sequence are stored and diffed run to
   run; any change beyond noise fails CI.
5. **Generality gate — required before "done":** `v_elite` (dual-wield), one single-hand
   weapon, and one weapon with 2-joint finger chains, all PASS.
6. **Performance budget.** Record wall-clock per sequence. A regression beyond a configured
   factor fails CI — this is what keeps the IK stage from silently degrading back into a
   brute-force search.

---

## 11. Decisions required from the author

These are judgement calls, not technical unknowns. Each is a recorded decision in the
config.

1. **Anchor policy.** Wrist-anchored with a floating forearm root (recommended, §7.4), or
   root-anchored with the wrist offset compensated by moving the weapon. Wrist-anchored
   preserves the grip; root-anchored preserves the arm's screen position.
2. **Weapon offset.** Confirm zero (recommended, §7.5), or authorise a single constant
   per-weapon offset with a sign-off on the numbers.
3. **Is "no re-rig" hard?** Constraint 2.1 generates essentially all of this project's
   difficulty. The alternative in §12.4 is dramatically simpler and grips perfectly by
   construction, at a real cost in deformation quality. This should be a decision, not an
   assumption.
4. **Frustum intrusion.** If a longer reference forearm's cut end enters frame, is that a
   FAIL, a warning, or handled by trimming geometry (which would violate 2.1)?
5. **Finger priority.** Confirm the relaxation order in §7.6 matches what reads worst
   on screen for this weapon.

---

## 12. Rejected alternatives

**12.1 Zero-overlap objective.** Retained from v1 and still correct: a real grip overlaps
the weapon heavily; the original hands intersect it by hundreds of triangles at idle.
Driving intersections to zero detaches the gun or slides it to the fingertips.

**12.2 Constraints plus visual-keying bake.** Rejected in favour of direct matrix keying
(§7.4). Constraints introduce evaluation-order and space-conversion failure modes, and
`COPY_TRANSFORMS` in particular leaks scale onto the reference hands, violating 2.1 and
2.3. Kept documented only as a fallback if direct keying proves impractical.

**12.3 Per-frame rigid weapon offset.** Rejected: re-authors weapon motion, desyncs
dual-wield guns and attachment points, and overrides art direction (§7.5).

**12.4 Re-binding the reference mesh to the original skeleton.** The standard modding
shortcut: skin the reference hand mesh to the `BoneNN` rig and play the animations
untouched. Grip is perfect by construction and the whole solver disappears. Cost: joint
rotation centres sit at the original skeleton's joint positions rather than at the
reference mesh's knuckles, so creases fall in the wrong places and the fingers deform
badly if the hands differ much in proportion. Forbidden by 2.1 as written — see §11.3.
This remains the escape hatch if the grip solve cannot converge on a given weapon.

**12.5 Scaling the reference hands to match the original.** The trivial solution, forbidden
by 2.1, and worth naming only so it is knowingly rejected rather than accidentally
reintroduced by a `COPY_TRANSFORMS` leaking scale — which Phase 6 now detects.

---

## Appendix A — Changes from v1

| v1 | v2 | Why |
|---|---|---|
| `COPY_ROTATION` / `COPY_TRANSFORMS` constraints | Direct matrix keying with per-bone rest correction `C(b)` | Rest orientations differ between rigs; constraints copy orientations, not animation (B1). `COPY_TRANSFORMS` also leaks scale (B2). |
| Penetration depth via `BVHTree` | Tip-position agreement in weapon space as the driver; unsigned distance and overlap counts as validators | `overlap()` yields no depth and signed distance is unreliable on an open shell — v1's own fallback was the right primary (B3). |
| Weapon offset solved per frame | Zero by default; at most one constant per weapon | Per-frame offsets re-author the animation and desync dual guns (B4). |
| "Delete the original hands" | Explicit skeleton unification, then deletion | The original armature drives the weapon; one SMD carries one node table (B5). |
| 1° increments until depth matches | Warm-started, regularised, joint-limited IK | The increment loop is infeasible at sequence scale and jitters (B6). |
| Match by world-X sign and rest position | Topology first, then Kabsch fit in a normalised frame; handedness from the palm normal | Viewmodel rigs are not symmetric about X = 0 and the two rigs share no pose (H2). |
| Facts "verified from the files" | Same facts asserted at runtime | Silent violations propagate into wrong output (H5). |
| — | Phase 6 post-export verification | Proves constraints 2.1–2.3 at format level and fixes Euler discontinuities headlessly (H1). |
| — | CLI, config, exit codes, failure policy, test strategy, version pinning | Specified as a CLI tool; none of it was defined (H8, M5–M8). |