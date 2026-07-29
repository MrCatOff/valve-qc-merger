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
weapon's animation behaviour. The weapon geometry, its bones and every
animation are preserved exactly; your reference hands are grafted onto the
weapon's wrist bones and their fingers are retargeted to follow the original
grip (Stage 1 uses forward-kinematics retargeting).

```bash
valve-qc-merger replace-hands path/to/v_anaconda \
    --hands path/to/reference_hands \
    --output path/to/v_anaconda_rehanded
```

The reference hands folder must contain `male.smd` and/or `female.smd` (with
their textures). The output folder is a ready-to-compile copy of the weapon
with a grafted reference SMD per hand variant, retargeted animations and an
updated QC.

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
