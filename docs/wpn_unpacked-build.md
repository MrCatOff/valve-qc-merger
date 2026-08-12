# Building the `tmp/wpn_unpacked` weapon pack

A worked record of assembling the decompiled CSO weapon dump under
`tmp/wpn_unpacked/` into compiled, combined GoldSource models — one clean
set per model kind:

| Prefix | Command | Role |
| --- | --- | --- |
| `p_*` | `merge-p` | player-held (3rd-person) weapons |
| `w_*` | `merge-w` | dropped-weapon world models |
| `v_*` | `merge-v` | first-person view-models |

The dump held **58 `p_`, 37 `w_`, 58 `v_`** model folders (each a
Crowbar-style decompile: one `.qc`, reference + animation SMDs, textures).

## 1. Group by prefix

Every merge command takes a *parent* directory and treats each subdirectory
with one `.qc` as a model, so the three kinds must be separated first. The
dump mixes them, so stage one directory of symlinks per prefix:

```sh
DUMP=tmp/wpn_unpacked
for grp in p w v; do
  mkdir -p build/$grp
  for m in "$DUMP"/${grp}_*/; do ln -s "$(cd "$m" && pwd)" "build/$grp/$(basename "$m")"; done
done
```

## 2. Merge each kind (excluding the models that can't ship)

A raw dump carries models that no merge or stock compiler can accept. They
are dropped with `--exclude <dir-name>`; the reasons are catalogued in
[§4](#4-excluded-models-and-why). The commands below produce a set that
passes every verification gate (`--dry-run` first to preview matches):

```sh
# world — 37 → 33 models, 2 parts
valve-qc-merger merge-w build/w --out out/world --name w_all \
  --exclude w_ak47_beast --exclude w_balbow --exclude w_buffm249 \
  --exclude w_m4a1g

# player — 58 → 50 models, 2 parts
valve-qc-merger merge-p build/p --out out/player --name p_all \
  --exclude p_ak47_beast --exclude p_buffm249 --exclude p_m3dragon \
  --exclude p_linkgun --exclude p_dupstebgun --exclude p_m4a1g \
  --exclude p_balrogm4 --exclude p_charger7

```

### View is a two-step pipeline: retarget → `merge-v --shared-hands`

The view-models must first wear **our** CSO hands, then merge under a single
shared hands bodygroup. Running `merge-v` on the raw dump instead would
canonicalise each weapon's *own* hands and emit them per-weapon — not what we
want. So:

1. **Retarget** every `v_` weapon onto our hands (`handswap`), one output
   folder each:

   ```sh
   for d in build/v/v_*/; do
     valve-qc-merger retarget --weapon-dir "$d" --out "out/rt/$(basename "$d")"
   done
   ```

   55/58 succeed; `v_ak47_beast`, `v_linkgun`, `v_stinger_frk14` have no
   usable hand rig and are skipped.

2. **Merge with `--shared-hands`** — emits ONE `hands` bodygroup (the retarget
   hands) shared by every weapon, keeping `pev_body` at hand+weapon.
   `--shared-hands` **rejects multi-part weapons** (>1 weapon submodel), whose
   extra weapon bodygroup would multiply `pev_body` past the 255 ceiling —
   they must ship on their own.

   ```sh
   valve-qc-merger merge-v out/rt --out out/view --name v_sh \
     --shared-hands --skip-unmatched --exclude v_m134ex
   ```

   Of 54 retargeted inputs (minus `v_m134ex`): **29 merged** into 2 parts,
   **20 multi-part weapons rejected**, 5 unmatched (one-handed / extra-arm
   rigs). Each part carries one shared hands bodygroup + one weapon bodygroup.

Each command auto-splits into numbered **parts** (`w_all_p1`, `w_all_p2`, …)
because stock `studiomdl` caps one model at 32 submodels. Every part lands in
its own self-contained subdirectory (`out/<kind>/p1/`, `p2/`, …) with its
QC, geometry SMDs (`geometry/`) and textures. All parts verify clean:
`tables_consistent`, `render_preserved` / `geometry_preserved` (bit-exact),
`pose_preserved` (≈0 u), `canonical_hands` (view), `placement_preserved`
(player, ≤1e-3 u), `budgets`.

## 3. Compile each part

`studiomdl` is run per part directory. Two normalisations are required for
the bundled macOS-native `studiomdl` (`tmp/studiomdl`):

1. **Line endings** — SMD/QC must be LF, not CRLF.
2. **Path separators** — the merge QCs reference submodels with a Windows
   backslash (`studio "geometry\w_foo"`); the macOS compiler reads that as a
   literal filename and reports *"./geometry\w_foo.smd doesn't exist"*.
   Rewrite `\` → `/` in the QC.

```sh
STUDIO="$PWD/tmp/studiomdl"
for qc in out/*/p*/*.qc; do
  dir=$(dirname "$qc"); base=$(basename "$qc" .qc)
  find "$dir" \( -name '*.smd' -o -name '*.qc' \) -exec perl -i -pe 's/\r\n/\n/g' {} +
  perl -i -pe 's{\\}{/}g' "$qc"
  ( cd "$dir" && "$STUDIO" "$base.qc" )   # -> <base>.mdl beside the QC
done
```

Result — **6 compiled models**:

| Kind | Models | Parts (`.mdl`) |
| --- | --- | --- |
| world | 33 | `w_all_p1` (3.5M), `w_all_p2` (148K) |
| player | 50 | `p_all_p1` (3.4M), `p_all_p2` (1.5M) |
| view (shared hands) | 29 | `v_sh_p1` (7.4M), `v_sh_p2` (4.0M) |

## 4. Excluded models, and why

The exclusions fall into four classes. None are merge bugs — they are
source-data limits (GoldSource caps, broken textures) or genuinely
non-standard rigs.

**Broken / missing textures** — the decompile stored a mojibake (mis-encoded
Cyrillic) BMP name the model can't reference:

- `*_m4a1g` — texture `m4a1_…[…]_p.BMP` absent (excluded from world +
  player; in view it is also one of the multi-part rejects).
- `p_dupstebgun` — texture `:REGA_DUPSTEBGUN1.bmp` absent.
- `v_m134ex` — its `v_m134ex_set.smd` has a mojibake **material line** the
  SMD parser rejects outright (it aborts the whole batch, so it must be
  excluded, not merely skipped).

**Over the GoldSource per-submodel geometry cap** (stock `studiomdl`):

- `*_ak47_beast` (~2500 v), `*_buffm249` (~2270 v), `w_balbow` /
  `p_m3dragon` (2127 v / 3757 v) — exceed **2048 vertices/normals**.
- `p_linkgun` — **5296 triangles**; compiles alone with *"too many normals
  in model"*. (The merge only *warns* on high triangle counts, so it clears
  verification but not the compiler — caught at step 3.)

All of these need a raised-limit compiler; excluded to keep the set
stock-compilable.

**Player hand-bind divergence** (`placement_preserved`, tol 1e-3 u):

- `p_balrogm4` (21 u) and `p_charger7` (6.6 u) — their `Bip01 R Hand` bind
  pose diverges from the shared canonical, so the merged model can't
  reproduce their original placement. (Which model trips depends on the
  32-submodel partition, so both are excluded together.)

**View drop-outs** — the view pipeline (retarget → `--shared-hands`) sheds
models at three points, none needing a manual `--exclude` except `v_m134ex`:

- **Retarget failed (3)** — no usable hand rig, so `handswap` can't wear our
  hands: `v_ak47_beast`, `v_linkgun`, `v_stinger_frk14`.
- **Multi-part rejected by `--shared-hands` (20)** — more than one weapon
  submodel, which would push `pev_body` past 255 once merged:
  `v_ak47chimera`, `v_ak47g`, `v_ak47lor`, `v_ancientjanus7`, `v_balbow`,
  `v_balrogm4`, `v_buffm249`, `v_charger7`, `v_dupstebgun`, `v_laserminigun`,
  `v_m32`, `v_m3dragon`, `v_m4a1g`, `v_m4a1s`, `v_plasmagun`, `v_rpg7`,
  `v_skull6`, `v_spsmg`, `v_stunrifle`, `v_vulcanus7`.
- **Correspondence unmatched (5)** — one-handed or extra-arm rigs
  `--skip-unmatched` drops: `v_awp_kraken`, `v_janus7`, `v_portal`,
  `v_rpg_remapped`, and `v_awpchimera` (rename collisions from a third arm).

Only `v_m134ex` (the mojibake-material parse crash above) needs an explicit
`--exclude`; the rest fall out on their own, so the build is deterministic.

## See also

- [`merge-v`](merge-v.md), [`merge-p`](merge-p.md),
  [`merge-w`](merge-w.md) — per-command reference.
