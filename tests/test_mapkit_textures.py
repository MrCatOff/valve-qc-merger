"""mapkit stage 3: pictures, texture replacement, embedding, extraction, shared WADs."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np
import pytest

from tests.mapkit_fixtures import build_map, texture
from valve_qc_merger.cli import main
from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.analyze import check_map
from valve_qc_merger.mapkit.fix import plan_embed, plan_extract, plan_replace_texture, save_plan
from valve_qc_merger.mapkit.geometry import geometry
from valve_qc_merger.mapkit.images import read_image
from valve_qc_merger.mapkit.textures import (
    extract,
    replace_embedded,
    shared_wad,
    texture_from_image,
    wad_of_lumps,
)
from valve_qc_merger.server.entities import parse
from valve_qc_merger.sprite.wad import read_wad, write_wad

RNG = np.random.default_rng(7)
PICTURE = RNG.integers(0, 256, (6, 5, 4), dtype=np.uint8)


def _png(rgba: np.ndarray, filters: list[int]) -> bytes:
    """An RGBA PNG whose rows use the given filters (forward-filtered)."""
    h, w = rgba.shape[:2]
    stride = w * 4
    rows = rgba.reshape(h, stride).astype(np.int32)
    raw = bytearray()
    prev = np.zeros(stride, np.int32)
    for y in range(h):
        kind = filters[y % len(filters)]
        line = rows[y]
        left = np.concatenate([np.zeros(4, np.int32), line[:-4]])
        corner = np.concatenate([np.zeros(4, np.int32), prev[:-4]])
        if kind == 0:
            out = line
        elif kind == 1:
            out = line - left
        elif kind == 2:
            out = line - prev
        elif kind == 3:
            out = line - ((left + prev) >> 1)
        else:
            p = left + prev - corner
            pa, pb, pc = np.abs(p - left), np.abs(p - prev), np.abs(p - corner)
            out = line - np.where((pa <= pb) & (pa <= pc), left, np.where(pb <= pc, prev, corner))
        raw += bytes([kind]) + bytes((out & 0xFF).astype(np.uint8))
        prev = line

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


def _tga_rle(rgba: np.ndarray) -> bytes:
    h, w = rgba.shape[:2]
    body = bytearray()
    for px in rgba[::-1].reshape(-1, 4):  # bottom-up, one raw packet each
        body += bytes([0]) + bytes([px[2], px[1], px[0], px[3]])
    return struct.pack("<BBBHHBHHHHBB", 0, 0, 10, 0, 0, 0, 0, 0, w, h, 32, 8) + bytes(body)


def _bmp8(indices: np.ndarray, palette: np.ndarray) -> bytes:
    h, w = indices.shape
    stride = (w + 3) & ~3
    rows = b"".join(bytes(indices[y]) + b"\0" * (stride - w) for y in range(h - 1, -1, -1))
    table = b"".join(bytes([b, g, r, 0]) for r, g, b in palette)
    offset = 14 + 40 + len(table)
    return (b"BM" + struct.pack("<IHHI", offset + len(rows), 0, 0, offset)
            + struct.pack("<IiiHHIIiiII", 40, w, h, 1, 8, 0, len(rows), 0, 0, len(palette), 0)
            + table + rows)


# -- pictures -------------------------------------------------------------------

@pytest.mark.parametrize("filters", [[0], [1], [2], [3], [4], [0, 1, 2, 3, 4]])
def test_png_rows_in_every_filter(filters: list[int]) -> None:
    assert np.array_equal(read_image(_png(PICTURE, filters)), PICTURE)


def test_tga_and_bmp(tmp_path: Path) -> None:
    (tmp_path / "a.tga").write_bytes(_tga_rle(PICTURE))
    assert np.array_equal(read_image(tmp_path / "a.tga"), PICTURE)
    palette = RNG.integers(0, 256, (256, 3), dtype=np.uint8)
    indices = RNG.integers(0, 256, (3, 5), dtype=np.uint8)
    rgba = read_image(_bmp8(indices, palette))
    assert np.array_equal(rgba[..., :3], palette[indices]) and (rgba[..., 3] == 255).all()


def test_a_picture_becomes_a_texture_of_the_same_size() -> None:
    picture = np.zeros((64, 64, 4), np.uint8)
    picture[..., 0] = 200
    picture[..., 3] = 255
    picture[:32, :, 3] = 0  # the top half see-through
    solid = texture_from_image("brick", picture, 32, 16)
    assert (solid.width, solid.height) == (32, 16) and len(solid.palette) == 256
    glass = texture_from_image("{glass", picture, 32, 32)
    idx = np.frombuffer(glass.indices, np.uint8).reshape(32, 32)
    assert (idx[:16] == 255).all() and (idx[16:] != 255).all()
    assert glass.palette[255] == (0, 0, 255)


# -- the map's textures ---------------------------------------------------------

def _game(tmp_path: Path) -> Path:
    game = tmp_path / "cstrike"
    (game / "maps").mkdir(parents=True)
    world = {"classname": "worldspawn", "wad": "\\cso\\ext.wad;\\cso\\unused.wad"}
    (game / "maps" / "zm_t.bsp").write_bytes(build_map([world]))
    (game / "ext.wad").write_bytes(write_wad([texture("wall_ext", (90, 90, 200), 64)]))
    (game / "unused.wad").write_bytes(write_wad([texture("other", (1, 2, 3))]))
    return game / "maps" / "zm_t.bsp"


def test_replacing_an_embedded_texture_changes_only_that_lump() -> None:
    bsp = B.Bsp.parse(build_map())
    before = B.Bsp.parse(bsp.write())
    picture = np.full((16, 16, 4), 255, np.uint8)
    replace_embedded(bsp, "FLOOR_B", texture_from_image("x", picture, 16, 16))
    after = B.Bsp.parse(bsp.write())
    assert [i for i in range(B.LUMPS) if after.lumps[i] != before.lumps[i]] == [B.TEXTURES]
    new = next(t for t in extract(after) if t.name == "floor_b")
    assert new.rgba()[0, 0, :3].tolist() == [255, 255, 255]
    with pytest.raises(ValueError, match="16x16"):
        replace_embedded(bsp, "floor_a", texture_from_image("x", picture, 32, 32))


def test_embedding_takes_the_textures_out_of_their_wads(tmp_path: Path) -> None:
    path = _game(tmp_path)
    plan = plan_embed(path)
    assert not plan.crc_safe and plan.needs_new_name
    result = save_plan(plan)
    assert result.map_name == "zm_t_sp"
    new = path.with_name("zm_t_sp.bsp")
    textures = geometry(B.Bsp.read(new)).textures
    assert all(t.embedded for t in textures)
    world = parse(B.Bsp.read(new).entity_text)[0]
    assert world["wad"] == ""  # nothing comes from a WAD any more
    report = check_map(new)
    assert not [i for i in report.issues if i.code in ("missing_wad", "missing_texture")]


def test_extracting_and_stripping(tmp_path: Path) -> None:
    path = _game(tmp_path)
    save_plan(plan_extract(path, "zm_t_tex.wad"))
    assert {t.name for t in read_wad(path.parent.parent / "zm_t_tex.wad")} == {"floor_a",
                                                                              "floor_b"}
    assert path.with_name("zm_t.bsp.bak").is_file()  # the map itself unchanged (same CRC)
    plan = plan_extract(path, "zm_t_tex.wad", strip=True)
    result = save_plan(plan)
    stripped = B.Bsp.read(path.with_name(f"{result.map_name}.bsp"))
    assert not any(t.embedded for t in geometry(stripped).textures)
    assert "zm_t_tex.wad" in parse(stripped.entity_text)[0]["wad"]
    report = check_map(path.with_name(f"{result.map_name}.bsp"))
    assert not [i for i in report.issues if i.code in ("missing_wad", "missing_texture")]


def test_replacing_an_external_texture_copies_its_wad(tmp_path: Path) -> None:
    path = _game(tmp_path)
    picture = path.parent / "new.png"
    red = np.zeros((8, 8, 4), np.uint8)
    red[..., 0], red[..., 3] = 230, 255
    picture.write_bytes(_png(red, [0]))
    plan = plan_replace_texture(path, "WALL_EXT", picture)
    assert plan.crc_safe and plan.needs_new_name  # a WAD players must get
    result = save_plan(plan)
    copy = path.parent.parent / "ext_zm_t.wad"
    assert copy.is_file() and (path.parent.parent / "ext.wad").is_file()
    wall = next(t for t in read_wad(copy) if t.name == "wall_ext")
    assert (wall.width, wall.height) == (64, 64) and wall.rgba()[5, 5, 0] > 200
    world = parse(B.Bsp.read(path.with_name(f"{result.map_name}.bsp")).entity_text)[0]
    assert "ext_zm_t.wad" in world["wad"] and "\\ext.wad" not in world["wad"]


def test_shared_wad_keeps_one_of_each() -> None:
    a = B.Bsp.parse(build_map(external=[]), "a")
    b = B.Bsp.parse(build_map(embedded=[texture("floor_a", (200, 60, 40)),
                                        texture("floor_c", (1, 1, 1))], external=[]), "b")
    c = B.Bsp.parse(build_map(embedded=[texture("floor_a", (0, 0, 0))], external=[]), "c")
    result = shared_wad([(a, []), (b, []), (c, [])])
    assert sorted(t.name for t in result.textures) == ["floor_a", "floor_b", "floor_c"]
    assert result.conflicts == ["floor_a"] and result.used_by["floor_a"] == ["a", "b", "c"]
    assert len(read_wad(wad_of_lumps(result.lumps))) == 3


def test_map_textures_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _game(tmp_path)
    assert main(["map", "textures", str(path), "list"]) == 0
    out = capsys.readouterr().out
    assert "wall_ext" in out and "ext.wad" in out and "embedded" in out
    assert main(["map", "textures", str(path), "prune-wads", "--dry-run"]) == 0
    assert "unused.wad" in capsys.readouterr().out
    assert main(["map", "textures", str(path), "replace", "nope", "x.png"]) == 3
