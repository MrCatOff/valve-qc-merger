# Technical Specification: Automated Weapon Animation Retargeting CLI

## Context & Objective

Act as an automated 3D technical animation engineer. Build a headless Python CLI
tool driven by Blender (`bpy`) and the Blender Source Tools (BST) add-on that
retargets a weapon's animations from its original hands onto a strict, immutable
reference skeleton (`reference_hands.smd`), resolving geometric clipping
mathematically — never by visual guessing.

## Strict Constraints

1. **Immutable reference data.** Never modify the `nodes` (bone hierarchy/names)
   or `triangles` (mesh geometry) of `reference_hands.smd`. The reference hands
   are the user's own hands and must not be scaled, reshaped, or re-rigged.
2. **Animation only.** The only permitted output changes are bone transform
   values (rotation/location) inside the `skeleton` frame data, plus the weapon's
   rigid placement.
3. **No visual guessing.** The grip is validated mathematically
   (`mathutils.bvhtree.BVHTree`), against a defined, ground-truthed acceptance
   criterion (see Phase 3), not by eye.

## Input Files

* **`tmp/hands/reference_hands.smd`** — the immutable reference hands. Standard
  `Bip01` biped hierarchy (`Bip01 L/R Forearm → Hand → Finger0..4` with
  sub-joints and `*Nub` tips), plus the hand mesh.
* **`tmp/pistols/view/v_elite/v_elite-PV.smd`** — the weapon mesh and its bones.
* **`tmp/pistols/view/v_elite/f_elite_Male_hand_Low.smd`** — the **original**
  hand mesh and skeleton. Used as the collision ground-truth envelope (Phase 3).
* **`tmp/pistols/view/v_elite/v_elite_anims/*.smd`** — the animation sequences
  (draw, idle, reload, shoot_*, …). Each carries the skeleton frame data.

## Critical Facts About the Data (verified from the files)

These govern the whole pipeline and correct several assumptions:

1. **One unified generic skeleton — no `Bip01` in the animations.** The weapon
   reference, the original hand mesh, and every animation share a single
   `Bone01…Bone65` skeleton. Hands and weapon are rigged together. There is **no
   name correspondence** between the reference `Bip01 *` bones and the original
   `BoneNN` bones — the mapping must be built **geometrically** (Phase 2a).

2. **Hand bones vs weapon bones are cleanly separable by mesh weights.** The
   original hand mesh weights only to the forearm/finger bones; the weapon mesh
   weights only to the gun sub-chains. Therefore:
   * **Hand bones** = the `BoneNN` the hand mesh weights to → retarget targets.
   * **Weapon bones** = every other `BoneNN` → played verbatim, carry the gun,
     never retargeted.

3. **Weapons may be multi-arm.** `v_elite` is dual-wield: two
   `forearm → wrist → (5 finger chains)` arms and two guns (each gun hangs off
   its forearm chain). The pipeline must discover **N arms** from the rig, not
   assume a single hand.

## Algorithmic Pipeline

### Phase 1 — Headless Import & Setup

1. Initialize a clean Blender scene via `bpy`.
2. Import `reference_hands.smd`, `v_elite-PV.smd`, and the target animation SMD.
3. The imported animation skeleton is the original hand + weapon rig; it drives
   both the original hands and the weapon at their exact relative offsets.

### Phase 2a — Geometric Bone Correspondence (NEW)

Build the `Bip01 ↔ BoneNN` map automatically from the rest pose. There are no
matching names, so match by geometry:

1. **Classify `BoneNN`.** Split bones into *hand* (weighted by the original hand
   mesh) and *weapon* (everything else, incl. the gun sub-chains).
2. **Pair arms.** Match each reference forearm/wrist to an original
   forearm/wrist by world-X sign (L/R) and world position.
3. **Match fingers.** Within each hand, match the 5 reference finger chains
   (`Finger0..4`) to the 5 original finger chains by rest position.
   `Finger0` = thumb, identified as the geometric outlier (base offset toward the
   palm); the remaining four ordered across the knuckles.
4. **Match joint levels** down each chain
   (`Finger1 → Finger11 → Finger12` ↔ its 2–3-joint original chain). Reference
   tip bones (`*Nub`) have no original counterpart and inherit their parent's
   rotation.

### Phase 2b — Bone Retargeting

1. For root/arm bones (forearm, wrist), apply a **`COPY_TRANSFORMS`** constraint
   targeting the mapped original `BoneNN`.
2. For finger bones, apply a **`COPY_ROTATION`** constraint only — no scale, no
   location — so the reference hand keeps its own proportions and bone lengths.
3. Weapon bones need no constraint: the imported animation already animates them,
   and the weapon mesh follows them.

### Phase 3 — Mathematical Collision Resolution (Crucial)

Because the reference hands differ in length from the original hands, the grip
may drive the weapon into the palm or the fingers through the frame. Resolve this
per frame with BVH collision — **but against the correct target.**

**Acceptance criterion (this replaces "iterate until `overlap()` is zero").**
Zero overlap is the *wrong* goal: a correct grip has heavy surface contact — the
original `f_elite_Male_hand_Low` overlaps the gun by hundreds of triangles at
idle. Driving intersections to zero detaches the gun from the hand (floating) or
slides it off the fingertips.

> **Ground truth = the original hands.** For each frame, build the original hand
> mesh's BVH in its retargeted pose and use it as the reference contact envelope.
> A frame is *clean* when the reference hand's penetration **depth** into the
> weapon matches the original hand's — i.e. the longer reference fingers rest on
> the **same weapon surface** the original fingers rested on — not when the
> intersection **count** reaches zero.

* **Step 3.1 — Weapon seat (palm).** Solve the rigid weapon offset that places
  the reference palm where the original palm sat (depth-matched to the original
  envelope). Prefer a computed rigid seat over pushing the weapon along a normal
  until overlap clears (which detaches it). Re-evaluate the dependency graph after
  moving.
* **Step 3.2 — Finger curl.** For each finger that penetrates beyond the original
  envelope, offset its local rotation (curling onto the surface) until its tip
  reaches the **original tip's** distance-to-surface — absorbing the extra length
  as wrap, not poke-through. Small increments (e.g. 1°), re-evaluating each step.

**Instrumentation.** Log per-frame, per-hand overlap counts and penetration
depths (reference vs original envelope) before and after correction, so the
solver's behavior is visible in data and regressions are catchable.

### Phase 4 — Baking & Export

1. Once every frame satisfies the Phase 3 criterion, bake the constraints into
   raw keyframes on the reference skeleton (Visual Keying, Clear Constraints).
2. Delete the original hands from the scene.
3. Export the combined result (reference hands + weapon) as a new sequence SMD
   via `bpy.ops.export_scene.smd()`.

## Open Questions / Risks

* **Open-shell weapon mesh.** The gun is a low-poly open shell; inside/outside and
  signed-distance tests can flip on it. This is *why* Phase 3 measures depth
  against the original-hand envelope (a known-good reference) rather than asking
  "inside or outside the gun." If depth against the envelope proves unstable, fall
  back to matching the reference fingertip position directly to the original
  fingertip position (pure point-to-point, no inside/outside test).
* **Rig discovery generality.** Phase 2a must generalize beyond `v_elite`
  (single-hand weapons, differing finger-joint counts). Validate against at least
  one non-dual weapon before considering it done.
