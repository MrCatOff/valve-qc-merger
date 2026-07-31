# Fixes 2 — response to stages/report.md §7 (weapon rotation lost in export)

Applied by the verification session itself. Gates green: **66 pytest passed,
ruff clean, mypy --strict clean**. Full pipeline re-run on v_elite: all 16
sequences EXPORTED, **verify PASS (11/11 checks), exit 0**, and the emitted
model independently re-verified (below). `tmp/verify/model/` regenerated from
the fixed run.

## The critical fix (report §7.1)

**`worker.build_unified`** now sets `rotation_mode = "XYZ"` on every appended
gun pose bone (they are created after the retarget loop's rotation-mode pass and
defaulted to QUATERNION, so `key_guns`'s matrix assignments updated the
quaternion while the `rotation_euler` keys recorded dead identity values).
**`worker.key_guns`** additionally asserts every gun bone is in XYZ mode before
keying, so this class of failure aborts instead of exporting garbage.

Result, measured from the emitted text against the source animations (idle,
draw, reload, shoot_right1; multiple frames; every gun bone):

| Metric | Before | After |
|---|---|---|
| Gun bone world rotation delta | ~124–166° | ≤ 0.001° |
| Gun bone world position delta | 0 (root) / ≤ 7 u (children) | ≤ 0.00004 u |
| Skinned weapon-mesh vertex delta (idle f4) | mean 2.3 u, max 3.6 u | mean 0.00003 u, max 0.0001 u |

Slide/trigger/reload sub-bone animation is restored (they were fully frozen).
Visual: `tmp/verify/model/compare_idle_f4.png` — the exported model and the
original now hold the pistols identically; regenerate with
`tmp/verify/model/render_smd.py <frame> <out.png>`.

## Gate hardening (report §7.2)

1. **`weapon_pose_matches_source`** (new check): for every weapon bone, per
   frame, the FK world transform reconstructed from the exported anim SMD must
   equal the source anim's within 0.05 u / 1.0° (pure text, no Blender). Wired
   through `finalize_export` using the worker report's `gun_bones`.
   **Red/green demonstrated:** run against the pre-fix output it fails exactly
   on this check (rot 125.4–136.5°); on the fixed output it passes.
2. **`euler_component_continuity`** (new check): any per-frame Euler *component*
   jump above π fails — after a correct unwrap every component is within π of
   the previous frame, so a larger jump means the unwrap was skipped or broken.
   This closes the "geodesic check can never see a naming flip" hole; the
   geodesic check remains for real motion teleports.
3. **`reference_rest_preserved`** (new check): the exported mesh's rest skeleton
   is compared bone-by-bone against the *input* `reference_hands.smd` in world
   space (FK), closing the self-referential-baseline hole in
   `hand_translation_frozen`. Finding from the real data: Blender's first
   edit-mode roundtrip re-derives locals from head/tail/roll, renaming the
   R-forearm roll by ~2e-4 rad and shifting the wrist's *local* translation by
   lever-arm × angle (~0.0018 u) while every bone's world rest is unchanged and
   the hand mesh is bit-identical — so the check compares world rest (tol 1e-3)
   and reports local re-decomposition as a warning.

## Driver/CLI corrections (report §7.4)

4. **No silent sequence loss:** `finalize_export` now hard-fails (verify
   `outputs_present`, exit 2) if the mesh SMD or any ok sequence's anim SMD is
   missing, instead of silently excluding it while the QC still references it.
   `export_mesh_smd` / `export_anim_smd` also assert the file BST wrote exists.
5. **Config wiring:** `config.epsilon` and the new `config.geom_tolerance` are
   now passed into `verify_export` (they were silently ignored).
6. **§8.3 (FAIL writes no output):** documented as deliberate deviation 9 in
   `progress.md` — outputs are kept on a FAIL (exit 2) as the debugging
   artifact; nothing consumes the output dir automatically. Quarantine-on-FAIL
   can be added if a consumer ever appears.

## Documentation

- `phase56.md` — the false "reproducing the source weapon world pose exactly"
  claim is corrected in place with a pointer here; `progress.md` gains
  deviations 9 (FAIL keeps outputs) and 10 (component-continuity invariant).

## New tests (4)

- `test_weapon_pose_matching_source_passes` / `test_weapon_rotated_from_source_fails`
  — the frozen-gun failure mode is constant-rotation, invisible to both
  continuity checks; the pose check catches it.
- `test_euler_component_wrap_fails_even_when_rotation_is_continuous` — a 2π
  naming wrap with zero geodesic delta fails the component check and passes the
  geodesic one.
- `test_reference_rest_drift_fails` — a drifted rest offset invisible to
  `hand_translation_frozen` fails against the input reference.

## Deferred (unchanged from before)

- Phase 4 BVH validators (overlap band, `d_max`) and §8 per-frame metric CSV.
- Non-zero `weapon_offset`; QC bodygroup/texture-path polish; `--jobs`.
