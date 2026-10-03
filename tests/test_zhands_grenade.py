"""zhands-grenade: zombie hands put on the bundled donor grenade."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from valve_qc_merger.project import Project
from valve_qc_merger.resources import resource_path
from valve_qc_merger.services.base import CollectingReporter
from valve_qc_merger.services.zhands_grenade import (
    DEFAULT_DONOR,
    ZhandsGrenadeOptions,
    grenade_name,
    run_zhands_grenade,
)


def test_grenade_name() -> None:
    assert grenade_name("v_heavy_knife") == "v_heavy_grenade"
    assert grenade_name("v_ghost_knife_alternate") == "v_ghost_grenade_alternate"
    assert grenade_name("v_witch") == "v_witch_grenade"


@pytest.fixture
def knife(tmp_path: Path) -> Path:
    """A zombie hand model: the bundled donor itself wears banshee hands."""
    target = tmp_path / "v_banshee_knife"
    shutil.copytree(resource_path(DEFAULT_DONOR), target)
    return target


def test_service_makes_a_grenade_from_zombie_hands(knife: Path) -> None:
    out = knife.parent / "v_banshee_grenade"
    result = run_zhands_grenade(ZhandsGrenadeOptions(knife_dir=knife, out=out),
                                CollectingReporter())
    assert result.ok
    qc = (out / "v_banshee_grenade.qc").read_text(encoding="latin-1")
    assert "$modelname" in qc and "v_banshee_grenade.mdl" in qc
    for sequence in ("idle", "pullpin", "throw", "deploy"):
        assert (out / "anims" / f"{sequence}.smd").is_file()
    # the zombie's own hand texture is kept, the frog bomb rides along, and
    # our stock hands texture is not left behind unused
    bmps = {p.name.lower() for p in out.glob("*.bmp")}
    assert "frogbomb.bmp" in bmps and "witch_hand.bmp" in bmps
    assert "hands.bmp" not in bmps


def test_project_derives_a_grenade(knife: Path, tmp_path: Path) -> None:
    project = Project.create(tmp_path / "pack")
    [asset] = project.import_decompiled(knife, kind="zhands")
    result, made = project.derive_asset(asset.name, "grenade")
    assert result.ok
    assert made.name == "v_banshee_grenade"
    assert made.derived["mode"] == "grenade"
    assert (project.root / made.path / "v_banshee_grenade.qc").is_file()
