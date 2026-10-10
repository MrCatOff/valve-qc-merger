"""Packs (merges of other models): told by content, unpacked on import."""

from __future__ import annotations

import shutil
from pathlib import Path

from valve_qc_merger.project import Project
from valve_qc_merger.project.model import is_pack, model_role, pack_sources
from valve_qc_merger.project.unpack import assign_sequences, unpack

_MDL = Path(__file__).parent / "examples" / "mdl"
_PACK = Path(__file__).parent / "examples" / "pack" / "pack_two.mdl"  # 2 x mini.mdl


def test_packs_are_told_by_their_submodel_names() -> None:
    assert pack_sources(_PACK) == ["v_alpha", "v_beta"]
    assert is_pack(_PACK) and model_role(_PACK) == "pack"
    assert not is_pack(_MDL / "mini.mdl")


def test_sequences_go_to_their_source() -> None:
    labels = ["idle1", "reload", "idle", "v_b__reload", "v_c__idle", "drop"]
    owned = assign_sequences(labels, ["v_a", "v_b", "v_c"])
    assert owned == {"v_a": [(0, "idle1"), (1, "reload")],
                     "v_b": [(2, "idle"), (3, "reload")],
                     "v_c": [(4, "idle"), (5, "drop")]}
    shared = assign_sequences(["ref_aim_knife", "walk"], ["p1", "p2"], player=True)
    assert shared["p2"] == [(0, "ref_aim_knife"), (1, "walk")]


def test_unpack_writes_each_source(tmp_path: Path) -> None:
    made = {m.name: m for m in unpack(_PACK, tmp_path)}
    assert sorted(made) == ["v_alpha", "v_beta"]
    alpha = (made["v_alpha"].folder / "v_alpha.qc").read_text()
    assert "$modelname v_alpha.mdl" in alpha and '$sequence "idle"' in alpha
    assert '$sequence "shoot"' in alpha
    # the merge dropped v_beta's sequences (identical to v_alpha's): the first
    assert made["v_beta"].notes and '$sequence "idle"' in (
        made["v_beta"].folder / "v_beta.qc").read_text()
    assert [m.name for m in unpack(_PACK, tmp_path / "again", skip={"V_ALPHA"})] == ["v_beta"]


def test_import_unpacks_what_is_not_here(tmp_path: Path) -> None:
    folder = tmp_path / "server" / "models"
    folder.mkdir(parents=True)
    shutil.copy(_PACK, folder / "pack_two.mdl")
    shutil.copy(_MDL / "mini.mdl", folder / "v_alpha.mdl")  # on its own as well
    project = Project.create(tmp_path / "pack")
    outcome = project.import_models([folder])
    assert sorted(a.name for a in outcome.added) == ["v_alpha", "v_beta"]
    assert outcome.unpacked == ["pack_two: 1 model(s)"]
    assert project.assets["v_beta"].source.endswith("pack_two.mdl#v_beta")
    assert project.assets["v_alpha"].source.endswith("v_alpha.mdl")  # the file itself
    again = Project.create(tmp_path / "pack2")
    again.import_models([folder])
    assert again.import_models([folder]).ignored == ["pack_two"]  # nothing new in it
