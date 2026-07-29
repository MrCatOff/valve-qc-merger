# valve-qc-merger

A command-line toolkit for working with **GoldSource** model assets — QC
scripts, SMD geometry/animation files and hitboxes — as used by
**Counter-Strike 1.6** models.

## Status

Early scaffolding. The project layout, CLI skeleton and tooling are in place;
the QC/SMD/hitbox features are being built on top.

## Requirements

- Python 3.10+

## Installation

Development install (editable, with tooling) into a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Usage

```bash
valve-qc-merger --help
# or, without installing:
python -m valve_qc_merger --help
```

### replace-hands

Swap a weapon view-model's hands for your own reference hands, reproducing the
weapon's animation behaviour. It is a *replacement*: the weapon's own wrist and
finger bones are removed and your hand bones take their place (the bone count
does not double). The gun geometry, its bones and every animation are preserved
exactly -- kept bones that hung off the old wrist (e.g. the palm the gun is
skinned to) are re-attached to your hand, and the gun's world motion is
unchanged. Fingers follow the original grip via forward-kinematics retargeting
(Stage 1; no IK solver yet).

```bash
valve-qc-merger replace-hands path/to/v_anaconda \
    --hands path/to/reference_hands \
    --output path/to/v_anaconda_rehanded
```

The reference hands folder must contain `male.smd` and/or `female.smd` (with
their textures). The output folder is a ready-to-compile copy of the weapon
with a replaced reference SMD per hand variant, retargeted animations and an
updated QC.

If your hands do not sit correctly on the grip, nudge them with a constant
alignment offset (rotation in degrees, then translation), given per hand. The
gun is compensated so it stays put while only the hand moves:

```bash
valve-qc-merger replace-hands path/to/v_anaconda --hands path/to/reference_hands \
    --left-offset  "0,0,0,0.5,-1,0" \
    --right-offset "0,0,10,0,0,0"
```

If the hands clip into the grip, push the gun geometry off them with
`--weapon-offset "x,y,z"` (translation units, in model space). The gun keeps its
animation; only its mesh shifts, so a small nudge opens a gap:

```bash
valve-qc-merger replace-hands path/to/v_anaconda --hands path/to/reference_hands \
    --weapon-offset "0,0,0.5"
```

Often the grip does not sit in the centre of the palm because the reference hand
and the weapon's own hand attach at slightly different bones. `--seat-grip`
fixes this automatically: it finds where the *original* palm touched the gun and
moves the gun so the reference palm holds the grip in the same place. Weapons
that already grip well are left untouched.

```bash
valve-qc-merger replace-hands path/to/v_elite --hands path/to/reference_hands \
    --seat-grip
```

By default each finger points parallel to the weapon's, so a finger that is
longer than the weapon's overshoots the grip. `--finger-ik` instead curls each
finger (CCD) so its tip lands on the weapon fingertip -- the grip contact point
-- wrapping the grip like the original hands regardless of finger length:

```bash
valve-qc-merger replace-hands path/to/v_elite --hands path/to/reference_hands \
    --seat-grip --finger-ik
```

To instead work out a clearance move (for a gun poking *through* a surface), use
`--weapon-clearance`. A hand gripping a weapon always overlaps it a little, so
the goal is not zero overlap but the *original* hands' overlap: the tool
measures how much the weapon's own hands sat inside it, then slides the gun until
the reference hands overlap it no more than that (plus a small margin). Weapons
whose original hands did not clip get no move at all.

`auto` also picks the slide direction (away from the hand); otherwise give the
grip-preserving direction the gun can slide (the barrel/forward axis, in model
space — a radial push into a cupped grip only makes it worse):

```bash
valve-qc-merger replace-hands path/to/v_elite --hands path/to/reference_hands \
    --weapon-clearance auto
# or pick the forward axis yourself:
valve-qc-merger replace-hands path/to/v_elite --hands path/to/reference_hands \
    --weapon-clearance "0,-1,0"
```

## Project layout

```
src/valve_qc_merger/
├── cli.py            # argparse entry point
├── commands/         # CLI subcommands (registry + base class)
├── models/           # in-memory QC / SMD / hitbox structures
└── parsers/          # readers that turn raw files into models
tests/                # test suite
```

## Development

```bash
pytest          # run the test suite
ruff check .    # lint
mypy            # type-check
```
