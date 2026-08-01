"""merge-view texture stage tests: BMP8 ops, downscale, atlas packing."""

from __future__ import annotations

from pathlib import Path

from valve_qc_merger.merge_view.atlas import (
    TextureOptions,
    downscale_textures,
    pack_textures,
)
from valve_qc_merger.merge_view.bmp8 import Bmp8, read_bmp8, write_bmp8
from valve_qc_merger.models.geometry import Vector2, Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd, Triangle, Vertex


def _bmp(width: int, height: int, index: int, colour=(200, 30, 30)) -> bytes:
    palette = [(0, 0, 0)] * 256
    palette[index] = colour
    return write_bmp8(Bmp8(width, height, palette,
                           bytearray([index]) * (width * height)))


def _mesh(material: str, uv: tuple[float, float] = (0.25, 0.75)) -> Smd:
    zero = Vector3(0.0, 0.0, 0.0)
    vertex = Vertex(bone=0, position=zero, normal=Vector3(0.0, 0.0, 1.0),
                    uv=Vector2(*uv))
    return Smd(
        nodes=[Node(0, "Bip01", -1)],
        frames=[Frame(0, (BonePose(0, zero, zero),))],
        triangles=[Triangle(material, (vertex, vertex, vertex))],
    )


def test_bmp8_roundtrip() -> None:
    original = _bmp(6, 3, 7, (10, 20, 30))
    image = read_bmp8(original)
    assert (image.width, image.height) == (6, 3)
    assert image.palette[7] == (10, 20, 30)
    again = read_bmp8(write_bmp8(image))
    assert again.pixels == image.pixels
    assert again.palette == image.palette


def test_downscale_bounds_size_and_keeps_colour(tmp_path: Path) -> None:
    (tmp_path / "big.bmp").write_bytes(_bmp(64, 32, 5, (120, 60, 200)))
    warnings: list[str] = []
    changed = downscale_textures(tmp_path, ["big.bmp"], 16, warnings)
    assert changed == 1 and not warnings
    image = read_bmp8((tmp_path / "big.bmp").read_bytes())
    assert max(image.width, image.height) == 16
    assert image.palette[image.pixels[0]] == (120, 60, 200)


def test_pack_four_textures_rewrites_uvs(tmp_path: Path) -> None:
    names = [f"t{i}.bmp" for i in range(4)]
    colours = [(250, 0, 0), (0, 250, 0), (0, 0, 250), (250, 250, 0)]
    for name, colour in zip(names, colours, strict=True):
        (tmp_path / name).write_bytes(_bmp(8, 8, 1, colour))
    meshes = [_mesh(name) for name in names]
    warnings: list[str] = []
    mapping = pack_textures(tmp_path, meshes, names, {}, TextureOptions(),
                            warnings)
    assert set(mapping) == set(names)
    atlas_name = mapping[names[0]].split(":")[0]
    assert (tmp_path / atlas_name).exists()
    assert not (tmp_path / names[0]).exists()
    # every mesh now references the atlas, with UVs inside its own quarter
    for mesh, name in zip(meshes, names, strict=True):
        tile = int(mapping[name].split(":")[1])
        row, col = divmod(tile, 2)
        vrow = 1 - row
        for t in mesh.triangles:
            assert t.material == atlas_name
            for v in t.vertices:
                assert col / 2 < v.uv.u < (col + 1) / 2
                assert vrow / 2 < v.uv.v < (vrow + 1) / 2
    # tile colours survive the shared palette: sample each quarter's centre
    atlas = read_bmp8((tmp_path / atlas_name).read_bytes())
    for name, colour in zip(names, colours, strict=True):
        tile = int(mapping[name].split(":")[1])
        row, col = divmod(tile, 2)
        x, y = col * 256 + 128, row * 256 + 128
        r, g, b = atlas.palette[atlas.pixels[y * 512 + x]]
        assert max(abs(r - colour[0]), abs(g - colour[1]),
                   abs(b - colour[2])) <= 8


def test_pack_exclusions(tmp_path: Path) -> None:
    for name in ("a.bmp", "b.bmp", "c.bmp", "d.bmp", "glow.bmp", "wrap.bmp"):
        (tmp_path / name).write_bytes(_bmp(8, 8, 1))
    meshes = [_mesh(n) for n in ("a.bmp", "b.bmp", "c.bmp", "d.bmp", "glow.bmp")]
    meshes.append(_mesh("wrap.bmp", uv=(2.5, 0.5)))  # tiling UVs
    warnings: list[str] = []
    mapping = pack_textures(
        tmp_path, meshes,
        ["a.bmp", "b.bmp", "c.bmp", "d.bmp", "glow.bmp", "wrap.bmp"],
        {"glow.bmp": "additive"},
        TextureOptions(no_pack=["d.*"]), warnings,
    )
    assert set(mapping) == {"a.bmp", "b.bmp", "c.bmp"}  # 3 leftovers still pack
    for name in ("d.bmp", "glow.bmp", "wrap.bmp"):
        assert (tmp_path / name).exists()


def test_masked_pack_reserves_index_255(tmp_path: Path) -> None:
    # two masked textures: half masked (index 255), half solid green
    palette = [(0, 0, 0)] * 256
    palette[1] = (0, 200, 0)
    palette[255] = (0, 0, 255)
    pixels = bytearray([1] * 32 + [255] * 32)
    data = write_bmp8(Bmp8(8, 8, palette, pixels))
    for name in ("m1.bmp", "m2.bmp"):
        (tmp_path / name).write_bytes(data)
    meshes = [_mesh("m1.bmp"), _mesh("m2.bmp")]
    warnings: list[str] = []
    mapping = pack_textures(
        tmp_path, meshes, ["m1.bmp", "m2.bmp"],
        {"m1.bmp": "masked", "m2.bmp": "masked"},
        TextureOptions(), warnings,
    )
    atlas_name = mapping["m1.bmp"].split(":")[0]
    assert atlas_name.startswith("atlasm")
    atlas = read_bmp8((tmp_path / atlas_name).read_bytes())
    assert atlas.palette[255] == (0, 0, 255)
    # top quarter of tile 0 = original masked half; centre = green half
    assert atlas.pixels[16 * 512 + 64] != 255  # opaque row
    r, g, b = atlas.palette[atlas.pixels[16 * 512 + 64]]
    assert g > 150
    assert atlas.pixels[200 * 512 + 64] == 255  # masked row stays index 255
