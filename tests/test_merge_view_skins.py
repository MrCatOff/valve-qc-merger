"""merge-v: $texturegroup skins become weapon entries; header guards."""

from __future__ import annotations

import configparser
import shutil
from pathlib import Path

import pytest

from valve_qc_merger.merge_view.skins import check_header
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.services.base import CollectingReporter
from valve_qc_merger.services.merge_view import MergeViewOptions, run_merge_view

_ANACONDA = Path("tests/examples/v_anaconda")
_SKINS = '''
$texturegroup "skinfamilies"
{
	{ "Anaconda_512.BMP" }
	{ "Anaconda_gold.bmp" }
	{ "Anaconda_512.BMP" }
}
'''


def test_check_header() -> None:
    assert check_header("$scale 1.0\n$flags 0\n$origin 0 0 0\n") == \
        check_header("")
    bad = check_header("$scale 1.5\n$origin 0 0 -2\n$flags 512\n")
    assert len(bad.rejects) == 2 and "$scale 1.5" in bad.rejects[0]
    assert "$origin 0 0 -2" in bad.rejects[1]
    assert bad.warnings == ["$flags 512 dropped (one model-wide value; the merged "
                            "model uses $flags 0)"]


@pytest.fixture()
def models(tmp_path: Path) -> Path:
    root = tmp_path / "in"
    target = root / "v_anaconda"
    shutil.copytree(_ANACONDA, target)
    qc = target / "v_anaconda.qc"
    qc.write_text(qc.read_text(encoding="latin-1").replace("$flags 0", "$flags 0" + _SKINS),
                  encoding="latin-1")
    shutil.copy(target / "Anaconda_512.BMP", target / "Anaconda_gold.bmp")
    return root


def _ini(out: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.optionxform = str  # keep anim_ names as written
    parser.read(out / "models.ini")
    return parser


def test_skin_rows_become_weapon_entries(models: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    reporter = CollectingReporter()
    result = run_merge_view(MergeViewOptions(models_dir=models, out=out), reporter)
    assert result.ok, result.failures
    assert any("skins -> v_anaconda_skin1" in line for line in reporter.lines)
    ini = _ini(out)
    # row 2 repeats the base textures: no entry for a skin that changes nothing
    assert set(ini.sections()) == {"v_anaconda", "v_anaconda_skin1"}
    base, gold = ini["v_anaconda"], ini["v_anaconda_skin1"]
    assert base["pev_body"] != gold["pev_body"]
    anims = {k: v for k, v in base.items() if k.startswith("anim_")}
    assert anims and anims == {k: v for k, v in gold.items() if k.startswith("anim_")}
    # the mesh SMDs (the folder also holds animation SMDs, glob order varies)
    materials = {t.material for path in (out / "v_anaconda_skin1").glob("*.smd")
                 for t in parse_smd_file(path).triangles}
    assert any("gold" in m.lower() for m in materials)
    assert not any("anaconda_512" in m.lower() for m in materials)
    assert all(g.passed for g in result.gates), [g for g in result.gates if not g.passed]


def test_skin_variants_can_be_turned_off(models: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = run_merge_view(MergeViewOptions(models_dir=models, out=out,
                                             skin_variants=False), CollectingReporter())
    assert result.ok and _ini(out).sections() == ["v_anaconda"]


def test_scaled_model_is_rejected(models: Path, tmp_path: Path) -> None:
    second = models / "v_big"
    shutil.copytree(models / "v_anaconda", second)
    (second / "v_anaconda.qc").rename(second / "v_big.qc")
    qc = second / "v_big.qc"
    qc.write_text(qc.read_text(encoding="latin-1").replace("$scale 1.0", "$scale 1.3"),
                  encoding="latin-1")
    out = tmp_path / "out"
    result = run_merge_view(MergeViewOptions(models_dir=models, out=out,
                                             standalone_rejects=False),
                            CollectingReporter())
    assert any("v_big" in f and "$scale 1.3" in f for f in result.failures)
    assert "v_big" not in _ini(out).sections()


def _edit_qc(directory: Path, old: str, new: str) -> None:
    qc = next(directory.glob("*.qc"))
    text = qc.read_text(encoding="latin-1")
    assert old in text
    qc.write_text(text.replace(old, new, 1), encoding="latin-1")


def test_activities_carried_blends_rejected_options_warned(models: Path,
                                                          tmp_path: Path) -> None:
    anaconda = models / "v_anaconda"
    _edit_qc(anaconda, '"v_anaconda_anims\\idle1"\n\tfps 16',
             '"v_anaconda_anims\\idle1"\n\tfps 16\n\tACT_VM_IDLE 1\n\torigin 0 0 2')
    blender = models / "v_blend"
    shutil.copytree(anaconda, blender)
    (blender / "v_anaconda.qc").rename(blender / "v_blend.qc")
    _edit_qc(blender, '"v_anaconda_anims\\shoot1"',
             '"v_anaconda_anims\\shoot1" "v_anaconda_anims\\shoot2"\n\tblend XR -45 45')
    out = tmp_path / "out"
    reporter = CollectingReporter()
    result = run_merge_view(MergeViewOptions(models_dir=models, out=out, skin_variants=False,
                                             standalone_rejects=False), reporter)
    assert any("v_blend" in f and "blends 2 animations" in f for f in result.failures)
    assert any("option 'origin' not carried (idle1)" in line for line in reporter.lines)
    qc = (out / "v_merged.qc").read_text(encoding="latin-1")
    assert "\tACT_VM_IDLE 1" in qc  # carried into the merged block
    assert _ini(out).sections() == ["v_anaconda"]
