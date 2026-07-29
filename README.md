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
