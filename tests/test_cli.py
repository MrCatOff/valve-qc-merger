"""Smoke tests for the CLI wiring."""

from __future__ import annotations

import pytest

from valve_qc_merger import __version__
from valve_qc_merger.cli import build_parser, main


def test_build_parser_returns_program_name() -> None:
    parser = build_parser()
    assert parser.prog == "valve-qc-merger"


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help_and_fails() -> None:
    assert main([]) == 2
