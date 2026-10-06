"""Models a merge cannot take ship as models of their own (merge-p, merge-w)."""

from __future__ import annotations

import configparser
import shutil
from pathlib import Path

import pytest

from valve_qc_merger.services.base import Reporter

_EXAMPLES = Path(__file__).parent / "examples"


class _Quiet(Reporter):
    def log(self, message: str) -> None:
        pass


def _ini(out: Path) -> configparser.ConfigParser:
    ini = configparser.ConfigParser()
    ini.read(out / "models.ini")
    return ini


def test_merge_p_ships_oversize_models_on_their_own(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from valve_qc_merger.services import merge_player
    models = tmp_path / "models"
    shutil.copytree(_EXAMPLES / "player", models)
    small = {d.name for d in models.iterdir()}
    sizes = {name: (10, 10) for name in small}
    sizes["p_elite"] = (99999, 99999)  # too big for stock studiomdl
    monkeypatch.setattr(merge_player, "submodel_size", lambda model: sizes[model.name])
    out = tmp_path / "out"
    result = merge_player.run_merge_player(
        merge_player.MergePlayerOptions(models_dir=models, out=out, name="p_pack"), _Quiet())
    assert result.ok and not result.failures
    qc = out / "standalone" / "p_elite" / "p_elite.qc"
    assert qc in result.outputs and '$modelname "p_elite.mdl"' in qc.read_text("latin-1")
    ini = _ini(out)
    elite = ini["p_elite"]
    assert elite["model"] == "p_elite.mdl" and elite["standalone"] == "1"
    assert "vertices in one submodel" in elite["reason"]
    assert not any(k.startswith("anim_") for k in elite)  # p_ plays the player's
    others = sorted(small - {"p_elite"})
    assert all(name in ini for name in others)
    off = merge_player.run_merge_player(
        merge_player.MergePlayerOptions(models_dir=models, out=tmp_path / "off",
                                        name="p_pack", standalone_rejects=False), _Quiet())
    assert any("p_elite" in f for f in off.failures)
    assert not (tmp_path / "off" / "standalone").exists()


def test_merge_w_ships_unbakeable_models_on_their_own(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from valve_qc_merger.services import merge_world
    models = tmp_path / "models"
    shutil.copytree(_EXAMPLES / "world", models)
    names = sorted(d.name for d in models.iterdir())
    broken = names[0]
    real = merge_world.bake_rendered_pose

    def bake(model):  # noqa: ANN001, ANN202
        if model.name == broken:
            raise ValueError("cannot bake the pose")
        return real(model)

    monkeypatch.setattr(merge_world, "bake_rendered_pose", bake)
    out = tmp_path / "out"
    result = merge_world.run_merge_world(
        merge_world.MergeWorldOptions(models_dir=models, out=out, name="w_pack"), _Quiet())
    assert not result.failures
    assert (out / "standalone" / broken / f"{broken}.qc") in result.outputs
    entry = _ini(out)[broken]
    assert entry["standalone"] == "1" and entry["reason"] == "not merged: cannot bake the pose"
