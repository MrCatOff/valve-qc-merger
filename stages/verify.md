# Verify

What to check, how to check it, and what is confirmed vs still open. Update the
checkboxes as you review.

## How to check

**Scrub the animation (best check).** Open a `.blend` in Blender, press
<kbd>Space</kbd> or drag the timeline; orbit with the middle mouse. The reference
hands are keyed across the whole sequence; the guns ride the original animation.
Toggle the original-hand mesh's eye icon in the outliner to compare.

- `tmp/verify/idle/idle.blend`
- `tmp/verify/draw/draw.blend`
- `tmp/verify/reload/reload.blend`
- `tmp/verify/shoot_right1/shoot_right1.blend`

**Quick stills (no Blender).** Files carry the rendered frame as a suffix
(`_f<N>`), e.g. idle uses `_f4`, draw `_f16`, reload `_f69`, shoot_right1 `_f20`.
- `tmp/verify/<seq>/ours_{front,side,top}_f<N>.png` — our result, 3 angles
- `tmp/verify/<seq>/original_front_f<N>.png` — shipped hands, same camera
- `tmp/verify/closeup/{ours,orig}_{front,side}.png` — grip close-ups

Regenerate after code changes:
`Blender --background --factory-startup --python /tmp/build_verify.py`

## To verify now (Phase 2a / 2b)

- [ ] **Left/right** — each hand is on the correct pistol (auto: `L→Bone26`,
      `R→Bone03`). If ever wrong on another weapon, set `swap_arms = true`.
- [ ] **Hand orientation** — forearms reach along the grip toward each weapon
      (along Y depth), not splayed sideways along X.
- [ ] **Palm faces the grip** — the palm is presented to the gun handle.
- [ ] **Animation tracks** — hands follow draw / reload / shoot, staying with
      their gun (compare `original_front` per sequence).
- [ ] **No floating/exploded triangles** at any frame.
- [ ] **Bone proportions** — reference hands are not scaled or reshaped.

## Known-not-yet-correct (expected)
- **Fingers are open / splayed, not curled** onto the grip — that is Phase 4
  (grip solve), not built yet.
- **Forearms are long** and may extend past a viewmodel screen edge — the
  reference forearms are longer than the original's. Open decision (below).

## Confirmed by the user
- Hands follow the animation. ✅
- (fix applied) Hand orientation — forearms reach the weapon, not X-splay.
- (fix applied) Left/right — correct hand on each pistol by default.

## Open decisions / questions
- [ ] **Forearms**: leave long (off-screen in-game) or trim toward the original's
      forearm length? (spec §7.4 frustum guard, §11.4)
- [ ] **Weapon placement** (Phase 3): keep zero offset (hands come to the weapon,
      recommended §7.5) or nudge the weapon?
- [ ] After Phase 4: does the finger wrap match the original's grip contact?

## Not yet verifiable (phases not built)
- Phase 4 grip quality (finger curl vs original envelope).
- Phase 5 exported SMDs compile and load in-game.
- Phase 6 text-level guarantees (node tables identical, hands not reshaped,
  Euler continuity, frame indices).
