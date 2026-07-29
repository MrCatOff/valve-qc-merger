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
