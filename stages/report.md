# Verification report — Phases 0–2b

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
