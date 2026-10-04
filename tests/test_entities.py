"""Map entities: read, edit, save as .ent or into the BSP."""

from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from valve_qc_merger.server import entities as ents
from valve_qc_merger.server.bsp import read_map_resources

TEXT = """{
"classname" "worldspawn"
"wad" "cs_dust.wad"
}
{
"classname" "armoury_entity"
"origin" "10 20 30"
"angles" "0 90 0"
"item" "13"
"count" "2"
}
{
"classname" "armoury_entity"
"origin" "-5 0 8"
"item" "3"
}
{
"classname" "info_player_start"
"origin" "0 0 0"
}
"""


def write_map(path: Path) -> Path:
    """A BSP v30 whose lumps are not in index order (the entity lump last)."""
    other = [bytes([i]) * (i * 3 + 1) for i in range(15)]
    ents_blob = TEXT.encode("latin-1") + b"\0"
    body, table = b"", [(0, 0)] * 15
    for index in list(range(1, 15)) + [0]:
        blob = ents_blob if index == 0 else other[index]
        table[index] = (124 + len(body), len(blob))
        body += blob
    path.write_bytes(struct.pack("<i", 30) + b"".join(struct.pack("<2i", *t) for t in table)
                     + body)
    return path


def _lumps(data: bytes) -> list[bytes]:
    return [data[o:o + n] for o, n in (struct.unpack_from("<2i", data, 4 + i * 8)
                                       for i in range(15))]


def test_read_classes_and_text(tmp_path: Path) -> None:
    found = ents.read_entities(write_map(tmp_path / "zm_test.bsp"))
    assert found.classes() == {"armoury_entity": 2, "info_player_start": 1, "worldspawn": 1}
    assert ents.parse(ents.entity_text(found.entities)) == found.entities


def test_replace_keeps_place_and_bsp_keeps_other_lumps(tmp_path: Path) -> None:
    path = write_map(tmp_path / "zm_test.bsp")
    found = ents.read_entities(path)
    box = ents.as_model(found.entities[1], "models/w_supplybox.mdl")
    assert box == {"classname": "cycler_sprite", "origin": "10 20 30", "angles": "0 90 0",
                   "model": "models/w_supplybox.mdl", "framerate": "1"}
    marker = ents.as_marker(found.entities[2], "vqm_supplybox")
    assert marker == {"classname": "info_target", "origin": "-5 0 8",
                      "targetname": "vqm_supplybox"}
    edited = [found.entities[0], box, marker]
    before = path.read_bytes()
    written, backup = ents.save_bsp(path, edited)
    assert backup == tmp_path / "zm_test.bsp.bak" and backup.read_bytes() == before
    after = written.read_bytes()
    assert _lumps(after)[1:] == _lumps(before)[1:]  # the map CRC skips only lump 0
    assert ents.read_entities(written).entities == edited
    assert read_map_resources(written).models == {"models/w_supplybox.mdl"}
    assert ents.models_of(edited) == {"models/w_supplybox.mdl"}
    ent = ents.write_ent_file(edited, tmp_path / "maps" / "zm_test.ent")
    assert ents.read_ent_file(ent) == edited


QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_entities_tab(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from valve_qc_merger.project import Project
    from valve_qc_merger.studio.server_window import ServerWindow
    game = tmp_path / "cstrike"
    (game / "maps").mkdir(parents=True)
    write_map(game / "maps" / "zm_test.bsp")
    project = Project.create(tmp_path / "pack")
    project.settings.game_dir = str(game)
    window = ServerWindow(project)
    try:
        panel = window.entities
        assert panel.map_box.currentText() == "zm_test" and len(panel.entities) == 4
        panel.select_class("armoury_entity")
        assert panel.table.rowCount() == 2
        panel.select_all_shown()
        assert panel.replace_selected("model", "models/w_supplybox.mdl") == 2
        assert panel.classes()["cycler_sprite"] == 2 and "armoury_entity" not in panel.classes()
        assert "model slots +1" in panel.status.text() and panel.changed()
        panel.select_class("info_player_start")
        panel.select_all_shown()
        assert panel.remove_selected() == 1
        ent = panel.save_ent()
        assert ent == game / "maps" / "zm_test.ent" and not panel.changed()
        assert [e["classname"] for e in ents.read_ent_file(ent)] == \
            ["worldspawn", "cycler_sprite", "cycler_sprite"]
        panel.load(game / "maps" / "zm_test.bsp")  # the .ent wins, as in ReHLDS
        assert panel.source == "ent" and len(panel.entities) == 3
        panel.select_class("worldspawn")
        panel.select_all_shown()
        assert panel.remove_selected() == 0  # never the world
        out = panel.save_bsp(tmp_path / "copy.bsp")
        assert out is not None and len(ents.read_entities(out).entities) == 3
    finally:
        window.close()
