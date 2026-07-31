# Phase 5 & 6 report — what to check

The pipeline now produces a **complete, compilable model** and verifies it at the
text level. One command retargets, unifies, exports and gates all 16 v_elite
sequences end-to-end. Gates green: **62 pytest passed, ruff clean, mypy --strict
clean** (28 source files). A ready-to-inspect run is in `tmp/verify/model/`.

## What was built

### Phase 5 — skeleton unification + export (§7.7)
- **`retarget/unify.py`** (pure, `bpy`-free, unit-tested): `discover_guns` finds
  each gun subtree (a non-hand bone whose parent is a hand bone, with weapon
  weight below it); `assign_wrists` maps each gun to the reference wrist of the arm
  whose wrist shares the gun root's parent — on v_elite the gun hangs off the
  *forearm*, so this walks arm ancestry rather than the gun's own chain. Enforces
  the §5 arm→gun bijection.
- **`worker.build_unified`**: appends the weapon bones into the reference armature
  in edit mode, copying each bone's world rest matrix (names, hierarchy, rest
  offsets preserved), re-parenting only each gun root onto its reference wrist, and
  rebinds the weapon mesh (its `BoneNN` vertex groups match by name unchanged).
- **`worker.key_guns`**: transfers the weapon animation per frame — the gun root's
  armature-space matrix is copied from the source (Blender back-solves the basis
  against the new wrist parent); deeper gun bones copy the source `matrix_basis`
  directly, reproducing the source weapon world pose exactly (Phase 3 zero-offset:
  the weapon stays where its own animation puts it, the hands come to it).
  **Correction (third verification pass):** as originally shipped this claim was
  false — the appended pose bones were left in Blender's default QUATERNION
  rotation mode, so the `rotation_euler` keys recorded dead identity values and
  the exported guns were frozen at rest orientation (~125° off at idle, slide and
  reload part motion lost). Fixed by forcing `rotation_mode = "XYZ"` on every
  appended bone in `build_unified` (asserted in `key_guns`), and the gate now
  proves weapon-pose fidelity from the emitted text (`weapon_pose_matches_source`).
  See `stages/fixes2.md`.
- **Export**: the original armature + original hand mesh are deleted, then the
  reference-hands + weapon meshes are exported merged into one rest-pose
  `<weapon>-PV.smd`, and the unified armature's action into `anims/<seq>.smd`.
- **`retarget/qc_build.py`** (beyond §7.7, for compilability): regenerates the QC
  pointing at the merged mesh + `anims/`, keeping sequences (fps + events) and the
  attachments whose bones survive, dropping `$hbox` lines that referenced the
  deleted arm bones.

### Phase 6 — post-export text verification (§7.8)
- **`retarget/euler_unwrap.py`** (pure): BST re-derives each frame's Euler from the
  pose matrix independently, so it can 2π-wrap or gimbal-flip across the |Y|=90°
  singularity. This unwraps every emitted animation track *in place* — picking, per
  bone per frame, the representation (principal, gimbal-equivalent, or 2π wrap)
  nearest the previous frame. The rotation is unchanged; only its naming is made
  continuous, which is what GoldSrc's per-component interpolation needs.
- **`retarget/verify_smd.py`** (pure): parses the written SMDs and proves the §2
  constraints. The driver runs it as a gate; a fail flips the CLI exit code.

## How to check it

1. **Run it yourself** (writes the whole model):
   ```
   python -m valve_qc_merger retarget \
     --reference tmp/hands/reference_hands.smd \
     --weapon-dir tmp/pistols/view/v_elite \
     --anims 'v_elite_anims/*.smd' \
     --out out/v_elite_myhands
   ```
   Expect `verify PASS (8/8 checks)`, a `v_elite-PV.qc`, and exit 0.

2. **Look at the pre-built output** in `tmp/verify/model/`:
   - `emitted_model.png` — the emitted model skinned straight from the SMD text
     (no Blender), across idle/reload/shoot: two hands gripping two pistols, the
     reload separating one gun, recoil on shoot.
   - `v_elite-PV.smd` (merged hands+weapon), `anims/*.smd`, `v_elite-PV.qc`,
     `summary.json` (per-sequence reports + the verify block).

3. **Compile** (optional, needs a GoldSrc `studiomdl`): run it on
   `tmp/verify/model/v_elite-PV.qc` to produce a `.mdl` and load it in-game /
   in a model viewer. This is the only step the automated gate can't do here.

## Phase 6 gate — the 8 checks (all pass)

| Check | Proves |
|---|---|
| `node_tables_identical` | mesh SMD and every anim share one node table |
| `reference_bones_preserved` | reference bone names + parents unchanged in the unified table |
| `hand_translation_frozen` | every hand bone (bar the forearm anchor) keeps its rest local translation in **every** frame — a direct proof the hands were not reshaped |
| `no_nan_inf` | solver sanity |
| `euler_continuity` | no per-frame geodesic rotation teleport (after the text unwrap) |
| `frame_indices` | frame times contiguous from 0 |
| `frame_count` | matches the source sequence |
| `reference_mesh_preserved` | every reference hand vertex survives within tolerance (measured 0.00000 u) |

## Documented deviations (warnings, not failures)
- **Reference bones not strictly first in the node table.** BST hierarchy-sorts, so
  a gun bone parented under a wrist nests inside that hand's subtree. Tables are
  still identical across all SMDs and reference names/parents/rest are unchanged.
- **Mesh preserved by position, not byte-identical.** A Blender round-trip
  recomputes normals; Phase 6 proves geometric preservation instead.

## Known limitations / next
- **QC merges hands+weapon into one body** — the original per-skin/weapon bodygroup
  toggles are not reproduced (single-variant model). Fine for one hand set.
- **Phase 4 BVH validators** (overlap band, `d_max` over-penetration) and the §8
  per-frame metric CSV are still open (see `stages/phase34.md`).
- **Non-zero `weapon_offset`** (rigid weapon reposition) remains rejected, not
  implemented — nothing needs it yet.

## New tests (14, all pass)
- `tests/test_unify.py` — gun discovery, wrist assignment, bijection guard (4).
- `tests/test_euler_unwrap.py` — 2π unwrap, gimbal-flip resolution + rotation
  preserved, continuous-track no-op (3).
- `tests/test_verify_smd.py` — clean pass + node-mismatch / hand-drift / teleport
  failures (4).
- `tests/test_qc_build.py` — fps/events parse, deleted-bone attachment drop, QC
  render (3).
