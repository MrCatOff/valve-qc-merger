"""mapkit stage 6: external tools — found, run (fakes here), a decompiled map rescaled."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.mapkit_fixtures import build_map, texture
from valve_qc_merger.cli import main
from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit import external as X
from valve_qc_merger.mapkit.geometry import geometry

posix = pytest.mark.skipif(os.name == "nt", reason="fake tools are POSIX scripts")

FACE = ("( 0 0 0 ) ( 0 64 0 ) ( 64 0 0 ) BIG_TEX [ 1 0 0 16 ] [ 0 -1 0 32 ] 0 0.5 0.25\n"
        "( 0 0 0 ) ( 0 64 0 ) ( 64 0 0 ) other [ 1 0 0 16 ] [ 0 -1 0 32 ] 0 1 1\n")


@pytest.fixture(autouse=True)
def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(X, "config_dir", lambda: tmp_path / "config")
    for name in X.TOOLS:
        monkeypatch.delenv(f"VQM_{name.upper()}", raising=False)


def _script(folder: Path, name: str, body: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(f"#!{sys.executable}\nimport sys, shutil, os\n{body}\n")
    path.chmod(0o755)
    return path


def test_rescaling_keeps_the_texture_size_on_the_wall() -> None:
    text, changed = X.rescale_textures(FACE, {"big_tex": 2.0})
    assert changed == 1
    first = text.splitlines()[0]
    assert first.endswith("[ 1 0 0 8 ] [ 0 -1 0 16 ] 0 1 0.5")
    assert text.splitlines()[1] == FACE.splitlines()[1]


def test_tools_are_found_in_settings_env_and_path(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    assert not X.find_tool("hlcsg").available
    exe = tmp_path / "bin" / "hlcsg.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    X.save_settings({"hlcsg": {"path": str(exe), "prefix": ["mywine"]}})
    tool = X.find_tool("hlcsg")
    assert tool.available and tool.command("m")[:2] == ["mywine", str(exe)]
    other = _script(tmp_path / "env", "anything", "")
    monkeypatch.setenv("VQM_HLBSP", str(other))
    assert X.find_tool("hlbsp").path == other
    assert X.options_in_help("usage:\n  -chart  show\n  --threads N\n") == ["--threads", "-chart"]


@posix
def test_compile_runs_every_step_and_reads_errors(tmp_path: Path) -> None:
    bins = tmp_path / "bin"
    fixture = tmp_path / "fixture.bsp"
    fixture.write_bytes(build_map())
    settings: dict[str, dict[str, object]] = {}
    for step in X.COMPILERS:
        body = "print('ok', sys.argv[1:])"
        if step == "hlrad":
            body = f"shutil.copy({str(fixture)!r}, sys.argv[-1] + '.bsp'); print('done')"
        settings[step] = {"path": str(_script(bins, step, body))}
    (tmp_path / "zm_new.map").write_text("{\n}\n")
    result = X.compile_map(tmp_path / "zm_new.map", settings=settings)
    assert result.bsp is not None and [r.ok for r in result.runs] == [True] * 4
    game = tmp_path / "game"
    first = X.install_map(result.bsp, game, "zm_new")
    second = X.install_map(result.bsp, game, "zm_new")
    assert (first.name, second.name) == ("zm_new.bsp", "zm_new_sp.bsp")
    settings["hlbsp"] = {"path": str(_script(bins, "badbsp", "print('Error: Exceeded "
                                              "MAX_MAP_CLIPNODES'); sys.exit(1)"))}
    failed = X.compile_map(tmp_path / "zm_new.map", settings=settings)
    assert failed.bsp is None and failed.errors == ["Error: Exceeded MAX_MAP_CLIPNODES"]
    assert X.parse_errors(failed.runs[-1])[0]["code"] == "compile_limit"


@posix
def test_shrinking_big_textures(tmp_path: Path) -> None:
    game = tmp_path / "game"
    (game / "maps").mkdir(parents=True)
    path = game / "maps" / "zm_big.bsp"
    path.write_bytes(build_map(embedded=[texture("big_tex", (200, 10, 10), 512),
                                         texture("floor_b", (10, 200, 10))], external=[]))
    bins = tmp_path / "bin"
    seen = tmp_path / "seen.map"
    settings: dict[str, dict[str, object]] = {
        "decompiler": {"path": str(_script(bins, "dec", f"open(sys.argv[2], 'w').write("
                                           f"'{{\\n\"classname\" \"worldspawn\"\\n' + "
                                           f"{FACE!r} + '}}\\n')")),
                       "args": ["{bsp}", "{map}"]}}
    for step in X.COMPILERS:
        body = "pass"
        if step == "hlrad":
            body = (f"shutil.copy(sys.argv[-1] + '.map', {str(seen)!r}); "
                    f"shutil.copy({str(path)!r}, sys.argv[-1] + '.bsp')")
        settings[step] = {"path": str(_script(bins, step, body))}
    result = X.shrink_textures(path, 256, settings=settings)
    assert result.shrunk == {"big_tex": (256, 256)}
    assert result.wad is not None and result.bsp is not None
    text = seen.read_text()
    assert '"wad" "zm_big_small.wad"' in text and "0 1 0.5" in text
    assert result.notes[0] == "1 faces rescaled"
    small = X.shrink_textures(path, 1024, settings=settings)
    assert small.notes == ["no texture over 1024"]
    assert geometry(B.Bsp.read(path)).textures[0].width == 512  # the original stays


def test_map_tools_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exe = tmp_path / "TrenchBroom"
    exe.write_bytes(b"")
    assert main(["map", "tools", "--set", "trenchbroom", str(exe)]) == 0
    out = capsys.readouterr().out
    assert f"trenchbroom  {exe}" in out and "hlcsg        not set up" in out
    assert X.load_settings()["trenchbroom"]["path"] == str(exe)
    assert main(["map", "decompile", "x.bsp", "--out", "x.map"]) == 3  # no decompiler
