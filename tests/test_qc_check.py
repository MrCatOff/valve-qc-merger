"""Preflight QC check: what studiomdl fails on, found before it runs."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from valve_qc_merger.services.base import CollectingReporter
from valve_qc_merger.services.compile import CompileOptions, run_compile
from valve_qc_merger.services.qc_check import check_qc

_SMD = (b'version 1\nnodes\n  0 "root" -1\nend\nskeleton\ntime 0\n  0 0 0 0 0 0 0\nend\n'
        b"triangles\n%s\n0 0 0 0 0 0 0 0 0\n0 0 0 0 0 0 0 0 0\n0 0 0 0 0 0 0 0 0\nend\n")


def _model(folder: Path, qc: bytes, files: dict[str, bytes]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (folder / "m.qc").write_bytes(qc)
    return folder / "m.qc"


def _messages(qc: Path, level: str = "error") -> list[str]:
    return [p.message for p in check_qc(qc) if p.level == level]


def test_a_clean_qc_has_no_problems(tmp_path: Path) -> None:
    qc = _model(tmp_path, b'$modelname "m.mdl"\n$cd "."\n$body "studio" "ref"\n'
                b'// $sequence "commented" "nope"\n'
                b'$sequence "idle" {\n "anims/idle"\n fps 30\n loop\n ACT_IDLE 1\n'
                b' { event 5004 1 "weapons/x.wav" }\n}\n'
                b'$sequence "shoot" "anims/shoot" fps 20 LX\n',
                {"ref.smd": _SMD % b"skin.bmp", "anims/idle.smd": b"", "anims/shoot.smd": b""})
    assert check_qc(qc) == []


def test_a_label_of_another_code_page_names_a_missing_file(tmp_path: Path) -> None:
    label = "� KakTyc".encode()
    qc = _model(tmp_path, b'$body "studio" "ref"\n$sequence "' + label + b'" {\n "a/'
                + label + b'"\n}\n',
                {"ref.smd": _SMD % b"skin.bmp", "a/" + "ï¿½ KakTyc.smd": b""})
    errors = _messages(qc)
    assert len(errors) == 1 and "non-ASCII" in errors[0] and "missing" in errors[0]
    assert any("not plain ASCII" in w for w in _messages(qc, "warning"))


def test_limits_and_materials(tmp_path: Path) -> None:
    studios = b"".join(b' studio "ref"\n' for _ in range(33))
    qc = _model(tmp_path, b'$bodygroup "weapon"\n{\n blank\n' + studios + b'}\n'
                b'$sequence "' + b"x" * 32 + b'" "ref"\n',
                {"ref.smd": _SMD % b"king cobra.bmp"})
    errors = _messages(qc)
    assert any("34 submodels > 32" in e for e in errors)
    assert any("has a space" in e for e in errors)
    assert any("32 bytes > 31" in e for e in errors)


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="case-sensitive file system")
def test_a_file_in_another_case_is_an_error_off_windows(tmp_path: Path) -> None:
    qc = _model(tmp_path, b'$body "studio" "Ref"\n', {"ref.smd": _SMD % b"a.bmp"})
    assert any("on disk as 'ref.smd'" in e for e in _messages(qc))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake compiler")
def test_compile_stops_before_studiomdl_on_an_error(tmp_path: Path) -> None:
    qc = _model(tmp_path / "m", b'$modelname "m.mdl"\n$body "studio" "missing"\n', {})
    fake = tmp_path / "studiomdl"
    fake.write_text("#!/bin/sh\necho ran\nprintf 'IDST' > m.mdl\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    reporter = CollectingReporter()
    result = run_compile(CompileOptions(qc=qc, studiomdl=fake), reporter)
    assert not result.ok and "missing" in result.failures[0]
    assert "    ran" not in reporter.lines
    assert run_compile(CompileOptions(qc=qc, studiomdl=fake, preflight=False), reporter).ok


def test_a_submodel_over_2048_vertices(tmp_path: Path) -> None:
    tris = b"".join(b"m.bmp\n" + b"".join(f"0 {i}.{k} 0 0 0 0 1 0 0\n".encode()
                                         for k in range(3))
                    for i in range(700))
    smd = (b'version 1\nnodes\n  0 "root" -1\nend\nskeleton\ntime 0\n  0 0 0 0 0 0 0\nend\n'
           b"triangles\n" + tris + b"end\n")
    qc = _model(tmp_path, b'$body "studio" "ref"\n$sequence "idle" "ref"\n', {"ref.smd": smd})
    assert any("2100 vertices" in e for e in _messages(qc))
