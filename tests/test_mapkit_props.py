"""mapkit stage 4: a map's prop models merged with merge-props."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.mapkit_fixtures import build_map
from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.fix import save_plan
from valve_qc_merger.mapkit.props import (
    collect,
    find_studiomdl,
    merge_props,
    model_key,
    stale_parts,
)
from valve_qc_merger.server.entities import parse

EXAMPLES = Path(__file__).parent / "examples" / "mdl"


def _prop(model: str, x: float, **keys: str) -> dict[str, str]:
    return {"classname": "cycler_sprite", "model": model, "origin": f"{x:g} 0 0", **keys}


def _game(tmp_path: Path) -> Path:
    game = tmp_path / "cstrike"
    (game / "maps").mkdir(parents=True)
    for folder in ("a", "b"):
        (game / "models" / folder).mkdir(parents=True)
        shutil.copy(EXAMPLES / "mini.mdl", game / "models" / folder / "mini.mdl")
    shutil.copy(EXAMPLES / "mini_ext.mdl", game / "models" / "ext.mdl")
    shutil.copy(EXAMPLES / "mini_extT.mdl", game / "models" / "extT.mdl")
    entities = [{"classname": "worldspawn"},
                _prop("models/a/mini.mdl", -100), _prop("models/a/mini.mdl", -50),
                _prop("models/b/mini.mdl", 0),
                _prop("models/ext.mdl", 50, rendermode="4", scale="2"),
                _prop("models/skinned.mdl", 100),  # nowhere
                _prop("models/b/mini.mdl", 150, skin="1"),
                {"classname": "cycler", "model": "models/a/mini.mdl", "origin": "0 100 0"}]
    path = game / "maps" / "zm_p.bsp"
    path.write_bytes(build_map(entities))
    return path


def test_model_keys() -> None:
    assert model_key("models/props/trash.mdl") == "props_trash"
    assert model_key("models\\D_tree01.mdl") == "d_tree01"


def test_collect_tells_what_merges_and_why_not(tmp_path: Path) -> None:
    props = {p.path: p for p in collect([_game(tmp_path)])}
    assert set(props) == {"models/a/mini.mdl", "models/b/mini.mdl", "models/ext.mdl",
                          "models/skinned.mdl"}  # cycler is left alone
    assert props["models/a/mini.mdl"].uses == [(0, 1), (0, 2)]
    assert props["models/a/mini.mdl"].key == "a_mini" and not props["models/a/mini.mdl"].reason
    assert props["models/b/mini.mdl"].reason.startswith("shown with body 0 / skin 1")
    assert props["models/skinned.mdl"].reason == "not found"


def test_entities_point_at_the_merged_model(tmp_path: Path) -> None:
    path = _game(tmp_path)
    game = path.parent.parent
    result = merge_props([path], game, "zm_p_props", studiomdl=None, verify=False)
    assert {p.key for p in result.merged} == {"a_mini", "ext"}
    assert len(result.parts) == 1 and result.parts[0].startswith("models/zm_p/zm_p_props_")
    assert result.slots_before == 4 and result.slots_after == 3
    saved = save_plan(result.plans[0], res=False)
    assert saved.crc_after == saved.crc_before  # only the entities changed
    entities = parse(B.Bsp.read(path).entity_text)
    a1, a2, b, ext = entities[1], entities[2], entities[3], entities[4]
    assert a1["model"] == a2["model"] == ext["model"] == result.parts[0]
    assert {a1["body"], ext["body"]} == {"0", "1"} and a1["body"] == a2["body"]
    assert ext["rendermode"] == "4" and ext["scale"] == "2"  # the rest stays
    assert b["model"] == "models/b/mini.mdl"  # skin 1: left as it was
    assert entities[7]["model"] == "models/a/mini.mdl"  # the cycler too


STUDIOMDL = find_studiomdl()


@pytest.mark.skipif(STUDIOMDL is None, reason="no studiomdl for this platform")
def test_compiled_props_stay_where_they_were(tmp_path: Path) -> None:
    path = _game(tmp_path)
    game = path.parent.parent
    result = merge_props([path], game, "zm_p_props", studiomdl=STUDIOMDL)
    assert {p.key for p in result.merged} == {"a_mini", "ext"}, [(p.key, p.reason)
                                                                  for p in result.models]
    part = game / result.parts[0]
    assert part.is_file() and part.read_bytes()[:4] == b"IDST"
    assert (game / "models" / "zm_p" / "zm_p_props.json").is_file()
    save_plan(result.plans[0], res=False)
    assert stale_parts(game, "zm_p", "zm_p_props") == []
    old = part.with_name("zm_p_props_00000000.mdl")
    old.write_bytes(b"IDST")
    assert stale_parts(game, "zm_p", "zm_p_props") == [old]  # no map names it
