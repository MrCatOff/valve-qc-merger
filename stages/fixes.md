# Fixes — response to stages/report.md

Applied the corrections from the verification report. Quality gates green
(`pytest` 42 passed, `ruff` clean, `mypy --strict` clean). The retarget math path
was not touched, so `tmp/verify/` renders are unchanged (regenerated to confirm;
full run still `RETARGETED` 9 frames, dry run `MAPPED`).

Per correction:

1. **Silent worker failure → hard failure.** `driver.run_sequence` now launches
   Blender with `--python-exit-code 1` (unhandled worker exception → non-zero
   exit) **and** treats "exit 0 but no report file written" as a failure,
   returning a `FAIL` / `worker-crash` result instead of a fabricated empty PASS.
   `subprocess.TimeoutExpired` is also caught and returned as a `timeout` failure.
   *Test:* `test_run_sequence_fails_when_worker_writes_no_report`.

2. **`--dry-run` implemented.** The flag flows through the CLI → job → worker; the
   worker runs import + discovery + correspondence, skips the retarget, and reports
   `status = "MAPPED"`, `frames = 0`. *Test:*
   `test_run_sequence_threads_dry_run_into_the_job`; verified on real v_elite
   (`MAPPED`, 0 frames).

3. **Thumb-signal disagreement aborts.** `_identify_thumb` now raises
   `CorrespondenceError` naming both signals' picks, per §7.3.4, instead of warning
   and guessing. (Returns a plain `int` now; callers simplified.)

4. **Mirroring check added.** `worker.assert_no_mirrors` runs after import on the
   reference and source rigs; any bone whose rest matrix has a negative determinant
   raises a discovery failure (exit 3), per §7.3.

5. **Remaining deviations documented** in `progress.md` (§"Deviations", items 3-5):
   (a) unmatched target joints are *held* rather than §7.3.6 proportional
   redistribution, and the hold is depth-based not `*Nub`-name-based — **deferred**
   (correct for v_elite; only matters for a genuinely shorter-fingered source);
   (b) hand-set closure broader than spec; (c) anchor fallback to the wrist.

6. **BST scale claim corrected.** The SMD import/export is 1:1 (the operator
   exposes no scale factor), so there is no BST scale knob to assert; the
   `enable_bst` docstring no longer claims an assertion it never made, and points to
   the real unit-scale assertion in `clean_scene`.

7. **Smaller fixes.**
   - CLI exit codes split: bad/missing inputs → 3 (discovery); the §5 node-table
     gate and Blender-not-found → 4 (environment).
   - Finger-count mismatch between paired arms → `CorrespondenceError` (discovery),
     instead of a raw `zip(strict=True)` crash.
   - `TimeoutExpired` handled (see correction 1).
   - `_newest_mesh_for` (name-order dependent) replaced with explicit
     before/after capture (`_only_new_mesh`); each import's new mesh is identified
     by set difference.
   - `test_two_arms_pair_by_...` renamed/commented to say "side", matching the code.
   - `verify.md` still filenames corrected to include the `_f<N>` frame suffix.

## Deferred (with reason)
- **§7.3.6 proportional joint redistribution** — documented as deviation 3; not
  needed for v_elite. Revisit for a weapon whose source fingers are shorter than
  the reference's.
- **Gun-subtree → arm bijection §5 check** — still absent (was already disclosed
  in progress.md); low risk given the weight-disjointness assertion already holds.

## New tests
- `test_run_sequence_fails_when_worker_writes_no_report`
- `test_run_sequence_threads_dry_run_into_the_job`
(both use a fake `blender` executable, so they run without real Blender.)
