# Verification report — Phases 0–2b

> **Update (fourth pass): the §7 defect is FIXED and re-verified — see §8.**
> Weapon world pose now matches the source to ≤0.001° / 0.00004 u in every
> sequence; gate hardened to 11 checks incl. `weapon_pose_matches_source`
> (red on the pre-fix output, green on the fixed one). Details in
> `stages/fixes2.md`. Phases 5/6 accepted.
>
> **Update (third pass, Phases 3–6): NO-GO — see §7.** The exported model does
> not hold the weapons correctly (gun rotation animation is lost in export);
> one mandatory fix plus gate hardening required before acceptance.
>
> **Update (second pass): corrections verified — cleared for Phase 3/4.**
> See §6 at the bottom for the fix-verification details. The original findings
> below are kept for the record.

Independent check of the claims in `stages/progress.md` against
`TECHNICAL_SPECIFICATIONS_V2.md`, following `stages/verify.md`. Method: re-ran
the quality gates, visually inspected every render pair in `tmp/verify/`, and
performed a line-level code-vs-spec conformance review of the whole
`src/valve_qc_merger/retarget/` package (plus `commands/retarget.py` and the
tests).

**Verdict: the work is substantially as claimed and the core math is correct,
but proceed to Phase 3/4 only after the corrections in §"Required corrections"
below. They are small, and two of them (silent worker failure, `--dry-run`
no-op) would undermine trust in every later phase's results if left in.**

---

## 1. Quality gates — all pass (re-run, not taken on faith)

- `pytest`: **40 passed** (0.17s) — matches the claim.
- `ruff check .`: clean.
- `mypy --strict src`: clean, 23 files.
- Git history matches the commit list in progress.md; working tree clean.

## 2. Visual verification (verify.md checklist)

Inspected `tmp/verify/{idle,draw,reload,shoot_right1}` (ours vs original, same
camera) and the `closeup` pair.

- [x] **Left/right** — correct hand on each pistol in every sequence;
      composition matches the originals.
- [x] **Hand orientation** — forearms reach along the grip toward each weapon
      (confirmed in the top view); no X-splay.
- [x] **Palm faces the grip** — palms are presented to the handles (front views
      and close-ups).
- [x] **Animation tracks** — draw f16, reload f69, shoot_right1 f20 all
      reproduce the original frame's arrangement (including the crossed-hands
      reload pose).
- [x] **No floating/exploded triangles** in any inspected frame.
- [x] **Bone proportions** — hands look unscaled/unreshaped; code review
      confirms no scale or rest-edit path exists (see §3).
- Fingers open/splayed and long forearms: present, **expected** (Phase 4 not
  built; forearm length is an open §11.4 decision).

Note: verify.md's still filenames drifted — actual files carry a frame suffix
(`ours_front_f4.png`, not `ours_front.png`).

## 3. Code-vs-spec conformance

| Area | Status | Notes |
|---|---|---|
| Phase 0 (§7.1) | ✅ verified | `frame_start=0`, FPS from config, unit-scale assert, BST enabled+asserted (`worker.py:65-81`). Gap: BST *import/export scale* is never asserted despite spec §3/§7.1 and the `enable_bst` docstring claiming it. |
| Phase 1 (§7.2) | ✅ verified | All four imports, APPEND onto weapon rig with attach assert, meshes bound to the right rigs. Rest matrices read from `data.bones` (pose-independent) — equivalent to a pre-pose snapshot. |
| §5 assertions | ✅ as claimed (partial) | Node-table text gate in driver, weight disjointness with `w_min`, hand-set closure, arm discovery. Gun-subtree→arm bijection confirmed absent — honestly disclosed. Closure is broader than spec (adds all non-weapon ancestors to root) — undocumented deviation. |
| Phase 2a (§7.3) | 🟡 partial | Core works and is well-tested, but **three spec requirements are silently absent** (see corrections 3–5). |
| Phase 2b (§7.4) | ✅ verified | `C(b)` delta form, per-frame composition, and the direct `matrix_basis` formula all match the spec exactly; anchor closed-form wrist solve independently re-derived and correct; no constraints, no scale anywhere; Nubs held at identity. The anatomical absolute-orientation deviation is coherently implemented (reduces to the spec's delta form when frames coincide) and unit-tested. |
| §2 non-negotiables | ✅ verified | Reference rig never renamed/reparented/rest-edited; only the source rig is renamed; translations keyed only on anchors; pure rotation matrices → no scale can leak. |
| Determinism (§6) | ✅ verified | Sorted globs, sorted children, (depth, name) topo order, sequential frames. |
| §9 CLI / §8 report | 🟡 partial, honestly labeled | Except `--dry-run`, which is accepted and **silently ignored** — not disclosed. |
| Phases 3–6 | ⬜ confirmed absent | Matches "not started". Note: no SMD export exists at all yet — the worker emits only the report JSON. |

Independent math check: the delta/anatomical correction algebra, the basis
formula, the anchor solve, and the `Transform` row-matrix conventions were all
re-derived by hand — no errors found.

## 4. Required corrections (before Phase 3/4)

Ordered by importance:

1. **Silent worker failure reports success** (`driver.py:161-172`). Blender
   exits 0 even when the worker script throws (missing `--job`, bad job JSON,
   import failure under a bad `VQM_PKG_ROOT` — all outside the worker's own
   try/except). The driver then fabricates an empty `SequenceResult` and the CLI
   exits 0/PASS. Fix: pass `--python-exit-code 1` (or equivalent) to Blender
   **and** treat "exit 0 but no report file written" as a hard failure in
   `run_sequence`. Add a test.
2. **`--dry-run` is a no-op** (`commands/retarget.py:54-56`; `args.dry_run`
   never read). Either implement it per §9 (import + discovery + correspondence,
   write the map, no solve/export) or remove the flag until it works. A flag
   that promises a safety behavior and does nothing is worse than no flag.
3. **Thumb-signal disagreement must abort, not warn**
   (`correspondence.py:169-173`). Spec §7.3.4 is explicit: "abort with a
   diagnostic rather than guessing". Raise the existing correspondence error
   with both signals' values in the message.
4. **Add the §7.3 mirroring check**: any arm bone matrix with negative
   determinant → explicit error (the spec warns that silent orientation
   inversion is the failure mode). The current `RigBone` (head/tail only) cannot
   express it — take the determinant from the imported rest matrices in the
   worker if that's where the data lives.
5. **Document the remaining undocumented deviations** in progress.md's
   deviations section (or implement them): (a) §7.3.6 proportional distribution
   of the source terminal rotation across unmatched target joints is absent —
   excess target joints are simply held, and the hold is depth-based rather than
   `*Nub`-name-based (fine for v_elite, a real gap for shorter-fingered
   sources); (b) hand-set closure broader than spec; (c) anchor fallback to the
   wrist when the hierarchy is deeper than forearm-off-root.
6. **Assert BST import/export scale = 1.0** or fix the `enable_bst` docstring
   which currently claims an assertion that doesn't exist (`worker.py:65-69`).
7. Smaller, fix opportunistically: environment/§5-gate `DriverError`s exit 3
   instead of 4 (`commands/retarget.py:67-69`); mismatched finger counts crash
   via `zip(strict=True)` (`correspondence.py:423`) as exit 4 instead of a
   discovery failure; `subprocess.TimeoutExpired` escapes as a raw traceback;
   `_newest_mesh_for` relies on `bpy.data.objects` name order
   (`worker.py:155-165`) — safe today, latent trap; the
   `test_correspondence.py:128-134` name/comment says "chirality" but the code
   pairs by side; update verify.md's still filenames.

## 5. Decision

- **Phases 0, 1, 2b: accepted.** Claims verified in code, by tests, and
  visually.
- **Phase 2a: accepted with corrections** (items 3–5).
- **Proceed to Phase 3 (weapon placement, zero-offset default) and Phase 4
  (grip solve) only after corrections 1–4 are in and 5–6 are either fixed or
  documented.** Rationale: Phase 4's accept/reject is numeric and depends
  entirely on the pipeline failing loudly; correction 1 currently allows a
  crashed worker to read as PASS.
- The open decisions in verify.md (forearm length §11.4, weapon offset §11.2)
  remain with the author and do not block the corrections.

**To the implementing agent:** apply the corrections above, keep the quality
gates green (pytest, ruff, mypy --strict), regenerate the `tmp/verify/` renders
if any behavior-affecting code changed, and then **write a report at
`stages/fixes.md`** listing, per correction number: what was changed (files and
approach), what was deliberately deferred and why, and any new tests added.
That report is the input for the next verification pass.

---

## 6. Fix verification (second pass) — all corrections confirmed

Verified `stages/fixes.md` against commit `49a4e4b` line by line; re-ran all
gates (**42 passed**, ruff clean, mypy --strict clean on 23 files).

| Correction | Verdict | Evidence |
|---|---|---|
| 1. Silent worker failure | ✅ fixed | `--python-exit-code 1` added to the Blender invocation; exit-0-with-no-report returns a `FAIL`/`worker-crash` result with a forced non-zero code; `TimeoutExpired` caught and returned as `FAIL`/`timeout` (`driver.py:run_sequence`). Covered by `test_run_sequence_fails_when_worker_writes_no_report` using a fake blender executable. |
| 2. `--dry-run` | ✅ implemented | Flag threads CLI → job JSON → worker; worker skips `retarget()`, reports `MAPPED`/0 frames, and the report already carries the full correspondence map + classification, satisfying §9's "writes the map". Covered by `test_run_sequence_threads_dry_run_into_the_job`. |
| 3. Thumb-disagreement abort | ✅ fixed | `_identify_thumb` now raises `CorrespondenceError` naming both signals' picks (§7.3.4). |
| 4. Mirroring check | ✅ added | `worker.assert_no_mirrors` checks every rest matrix's 3×3 determinant on both rigs right after import; negative → `DiscoveryFailure` (exit 3). Runs in `bpy`, so not unit-testable outside Blender — acceptable. |
| 5. Deviations documented | ✅ done | progress.md deviations 3–5 now cover the held-joint behavior (§7.3.6), the broader hand-set closure, and the anchor fallback. §7.3.6 redistribution deferred with a stated reason (not needed for v_elite) — acceptable. |
| 6. BST scale claim | ✅ resolved | Docstring corrected: the SMD operator exposes no scale factor (I/O is 1:1); the real unit-scale assertion lives in `clean_scene`. |
| 7. Smaller fixes | ✅ all landed | Exit codes split (inputs→3; §5 gate and Blender-not-found→4 — a sensible reading of §9); finger-count mismatch now a `CorrespondenceError`; `_only_new_mesh` uses before/after set difference instead of name order; side-pairing test renamed to match the code; verify.md filenames carry the `_f<N>` suffix. |

Additional checks: `SequenceResult.ok` is `exit_code == 0` and both new failure
paths force a non-zero code, so a crash can no longer read as PASS. The
`tmp/verify/` renders were regenerated at commit time; the idle front render is
pose-identical to the pre-fix one, confirming the claim that the retarget math
path was untouched.

Residual notes (non-blocking, fold into later phases):
- No unit test exercises the thumb-disagreement abort or the finger-count
  mismatch error; add synthetic-rig cases when Phase 2a is next touched.
- The gun-subtree→arm bijection §5 check remains deferred (disclosed since the
  first pass).

**Decision: all first-pass conditions are met. Proceed to Phase 3 (weapon
placement, zero-offset default) and Phase 4 (grip solve).** On completion,
write `stages/progress.md` updates plus a `stages/phase34.md` report covering:
what was implemented per spec section (§7.5, §7.6), the solver parameters used,
per-sequence metric summaries (`e_pos`, overlap band, `d_max` guard), any
relaxations applied, and regenerated `tmp/verify/` renders — that is the input
for the next verification pass.

---

## 7. Phase 3–6 verification (third pass) — **NO-GO: the exported weapon is not held correctly**

Scope: commits `fc5f665` (Phases 3/4) and `2f2d84b` (Phases 5/6), claims in
`stages/phase34.md` / `stages/phase56.md`. Method: re-ran the gates (**62
passed**, ruff clean, mypy --strict clean), re-ran the full pipeline end-to-end
(all 16 sequences EXPORTED, verify PASS 8/8, exit 0 — reproduces the claim),
line-level code review of the new modules, **and an independent render of the
emitted SMD text** — both meshes skinned in pure Python straight from
`tmp/verify/model/` (no Blender, no pipeline code), side-by-side with the
original model posed by the original idle animation. Artifacts:
`tmp/verify/model/compare_idle_f4.png` (the render) and
`tmp/verify/model/render_smd.py` (regenerate with any frame).

### 7.1 The critical defect

**The exported weapons do not follow their animation; rotation is lost
entirely.** From the emitted text, independently confirmed two ways (my
FK reconstruction and the reviewer's):

- Every gun bone's **local rotation equals its rest rotation in every frame of
  every sequence** (delta = 0.000°). Only `location` was keyed.
- Gun-root world **positions** are exact (0.000 vs source, every frame), but
  world **orientations** are off by ~124–166° depending on the wrist pose; gun
  sub-bones land up to ~7 units off. Skinned weapon-mesh vertices are a mean
  2.3 u (max 3.6 u) from where the original puts them — on a pistol ~8 u long.
- Visually (`compare_idle_f4.png`): the original pistols point forward along
  the grip; ours stand ~vertical at the wrists. The hands themselves are
  correct — curled at the *source* weapon's grip — so the hand grips where the
  gun should be while the gun is rotated out of the grip. Slide recoil and
  reload part motion are also gone (those bones are fully frozen).

**Root cause (both reviews agree, high confidence):** `retarget()` sets
`rotation_mode = "XYZ"` only on pose bones that exist at that point
(worker.py:379-380). `build_unified` creates the gun pose bones *later*; they
default to `QUATERNION`. `key_guns` (worker.py:493-506) assigns
`pose_bone.matrix` / `matrix_basis` — which decompose into
`rotation_quaternion` — then keys `"rotation_euler"`, recording untouched
identity eulers. Location decomposes into `location` regardless of rotation
mode, which is exactly why translations survived and rotations did not.

**Fix:** set `rotation_mode = "XYZ"` on every appended gun pose bone (in
`build_unified`, before `key_guns`), then re-export and re-verify. One line,
plus the gate check below so this class of failure can never pass again.

### 7.2 Why the 8/8 gate said PASS on a broken model

The Phase 6 gate proves the *hands* are intact (translation frozen, mesh
preserved at 0.00000 u — both genuinely hold, confirmed) but has blind spots
exactly where the pipeline broke:

1. **Nothing compares the exported weapon pose to the source.** Required new
   check: for every weapon bone, per frame, FK world matrix from the exported
   anim == FK world matrix from the source anim within ε (both computable from
   text; my scratch check in `render_smd.py` shows how). This turns today's
   failure into a red gate.
2. **`euler_continuity` measures geodesic rotation delta, not per-component
   jumps** (verify_smd.py:188-216) — it cannot fail on the Euler-naming flips
   it nominally gates (a frozen-rotation track passes trivially). Add a
   per-component continuity check on the emitted numbers, per spec §7.8.
3. **Rest offsets are never compared to the input reference** —
   `hand_translation_frozen` baselines against the exported mesh's own frame 0
   (verify_smd.py:145-149), so a uniformly drifted rest would self-consistently
   pass, and phase56.md's "rest unchanged" claim is unproven. Compare the
   exported mesh SMD's rest locals against the input `reference_hands.smd`.

### 7.3 What did verify clean

- **Phase 3** (zero offset, non-zero rejected): verified (config.py:79,
  worker.py:620-624). Nit: `[0,0,0]` is rejected as "non-zero".
- **Phase 4 core CCD**: math independently checked and correct (conjugation
  into basis space, spaces, warm start, Nub exclusion, joint-limit clamping);
  the deferred spec parts (w_d, λ_t/λ_b, relaxation order, both BVH
  validators, e_pos normalisation) are genuinely absent and **honestly
  disclosed** in phase34.md. Note the ±150° default limits are effectively
  unconstrained — disclosed, revisit at §8.4 calibration.
- **Phase 5 structure**: unification itself is right — gun subtrees appended
  with world rest matrices preserved (0.000 delta verified from the emitted
  text), only gun roots reparented, weights carried by name, animation keyed
  before originals deleted, reference bones untouched (§2 holds).
- **Phase 6 machinery**: checks are wired as a real gate (fail → exit 2) and
  the euler_unwrap math (gimbal-equivalent `(x+π, π−y, z+π)` under Rz·Ry·Rx) is
  algebraically correct.
- Both stage reports' test/lint counts reproduce (62/62, clean, clean).

### 7.4 Additional corrections (from the code review)

4. **Silent sequence loss** (driver.py:233-237): an ok worker whose anim SMD
   is missing under the expected name is silently excluded from verification
   (exit stays 0) while the QC still references it. Make a missing SMD for an
   ok sequence a hard failure; have `export_anim_smd` assert the file exists.
5. **§8.3 failure policy**: on verify FAIL the CLI exits 2 but all outputs stay
   written; spec says FAIL writes nothing. Either quarantine outputs on FAIL
   (e.g. write to a temp dir, promote on PASS) or document the deviation.
6. `config.epsilon` is never passed to `verify_export` (1e-4 default used);
   `geom_tolerance` hardcoded. Wire them through.
7. phase56.md corrections: the "reproducing the source weapon world pose
   exactly" and "recoil on shoot" claims are false as shipped — rewrite after
   the fix; `emitted_model.png` (sparse scatter) is not evidence of grip or
   orientation, keep `compare_idle_f4.png`-style skinned renders instead.

### 7.5 Decision

- **Phases 3 and 4 (core): accepted** as scoped, deferrals disclosed.
- **Phase 5: rejected — correction 1 (rotation_mode) is mandatory**; the
  shipped model does not hold the weapons in line with the original.
- **Phase 6: accepted as machinery, but corrections 1–3 of §7.2 are required**
  before the gate's PASS can be trusted; correction 4 strongly recommended now,
  5–7 may follow.
- Re-verification will repeat the independent SMD-text render and the numeric
  weapon-pose comparison; acceptance requires gun world matrices to match the
  source within ε in all sequences, and the new gate check red/green to be
  demonstrated (break it deliberately in a test).

**To the implementing agent:** apply §7.1's fix and §7.2's gate checks (plus
§7.4 items 4–6 or a stated deferral), re-run the full pipeline, regenerate
`tmp/verify/model/`, and write `stages/fixes2.md` describing per item what was
changed, with the new gate shown failing on the pre-fix output (or an
equivalent test) and passing on the fixed output. That report is the input for
the next verification pass.

---

## 8. Fix verification (fourth pass) — weapon defect fixed, Phases 5/6 accepted

The §7 corrections were applied in-session (see `stages/fixes2.md` for the full
account). Verification of the result:

- **Gates:** 66 pytest passed, ruff clean, mypy --strict clean.
- **Red/green:** the hardened gate run against the *pre-fix* output fails
  exactly one check — `weapon_pose_matches_source` (rot 125.4–136.5°) — with
  the benign local-rest re-decomposition correctly demoted to a warning. The
  fixed pipeline run passes **11/11 checks, exit 0** on all 16 sequences.
- **Independent numeric check** (pure-Python FK from the emitted text, no
  pipeline code): every gun bone's world transform matches the source within
  **0.00004 u / 0.001°** across idle, draw, reload and shoot_right1 (was 2.3 u
  mean / ~125° before). Skinned weapon-mesh vertices at idle f4 match within
  0.0001 u.
- **Independent visual check:** `tmp/verify/model/compare_idle_f4.png`
  (regenerated) — the exported model and the original now hold the pistols
  identically in all three views; slide/reload sub-bone animation is restored.
- The one-bone rest "drift" flagged in the first red run was root-caused to
  Blender's edit-mode local re-derivation (parent roll renamed by ~2e-4 rad,
  world rest unchanged, hand mesh bit-identical) — a naming artifact, not a
  reshape; the new `reference_rest_preserved` check therefore proves world-rest
  preservation and warns on local re-decomposition.

**Decision: Phases 5 and 6 are now accepted.** The exported SMD holds the
weapon correctly, in line with the original, and the gate can no longer pass a
model that doesn't. Remaining open items are the previously disclosed
deferrals: Phase 4 BVH validators + §8 metrics CSV, the §10 generality gate
(second weapon, 2-joint-finger weapon), non-zero weapon offset, and an actual
`studiomdl` compile check of the generated QC.
