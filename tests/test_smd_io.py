"""Round-trip tests for the SMD parser and writer."""

from __future__ import annotations

from pathlib import Path

import pytest

from valve_qc_merger.parsers.smd import parse_smd_text
from valve_qc_merger.writers.smd import write_smd_text

_REFERENCE_SMD = """version 1
nodes
0 "root" -1
1 "Bip01_L_Hand" 0
end
skeleton
time 0
0 0.000000 0.000000 0.000000 0.000000 0.000000 0.000000
1 1.500000 0.000000 0.000000 0.100000 -0.200000 0.300000
end
triangles
male.bmp
0 1.000000 2.000000 3.000000 0.000000 0.000000 1.000000 0.250000 0.750000
1 -1.000000 0.000000 0.500000 0.000000 1.000000 0.000000 0.100000 0.900000
0 0.000000 4.000000 0.000000 1.000000 0.000000 0.000000 0.500000 0.500000
end
"""


def test_reference_smd_round_trip() -> None:
    smd = parse_smd_text(_REFERENCE_SMD)
    assert smd.bone_count == 2
    assert smd.node_by_name("Bip01_L_Hand") is not None
    assert smd.frame_count == 1
    assert smd.is_reference
    assert smd.materials() == ["male.bmp"]
    # Writing then re-parsing must reproduce the same structure.
    reparsed = parse_smd_text(write_smd_text(smd))
    assert reparsed == smd


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _sample_smds() -> list[Path]:
    base = _repo_root() / "tmp"
    if not base.exists():
        return []
    return [
        base / "hands" / "male.smd",
        base / "pistols" / "view" / "v_anaconda" / "ref_Anaconda.smd",
        base / "pistols" / "view" / "v_anaconda" / "v_anaconda_anims" / "draw.smd",
    ]


@pytest.mark.parametrize("path", _sample_smds(), ids=lambda p: p.name)
def test_real_smd_round_trips_semantically(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"sample not available: {path}")
    original = parse_smd_text(path.read_text(encoding="latin-1"))
    reparsed = parse_smd_text(write_smd_text(original))
    assert reparsed.nodes == original.nodes
    assert reparsed.frame_count == original.frame_count
    assert len(reparsed.triangles) == len(original.triangles)
    # Re-serializing the reparsed model is now byte-stable.
    assert write_smd_text(reparsed) == write_smd_text(original)
