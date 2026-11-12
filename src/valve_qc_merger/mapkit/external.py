"""Other people's map tools, found and run: the compilers, bspguy, a BSP
decompiler, TrenchBroom.

This tool has no compiler of its own: what needs geometry changed — a
recompile, a texture shrunk, geometry past ±4096 — goes through tools the
user installs:

| tool | what for |
|---|---|
| ``hlcsg`` ``hlbsp`` ``hlvis`` ``hlrad`` (VHLT / SDHLT) | ``.map`` -> ``.bsp`` |
| ``bspguy`` | merge, clean, move, export maps |
| ``decompiler`` (any BSP -> MAP tool) | an editable ``.map`` (brushes approximate) |
| ``trenchbroom`` | editing a ``.map`` (Valve 220) |

A tool is looked up in the settings file (``map-tools.json`` in the user's
config folder: ``{"hlcsg": {"path": …, "args": […], "prefix": ["wine"]}}``),
then ``VQM_<TOOL>``, then ``PATH``. A Windows ``.exe`` on macOS / Linux runs
through ``prefix`` (``wine``, CrossOver's). Tools differ between versions, so
no command line is assumed beyond the compilers' ``<tool> [options] <map>``:
:func:`tool_help` reads a tool's own ``--help`` for the options it has, and
the decompiler's arguments are a template (``{bsp}``, ``{map}``) the user
sets.

Everything runs on a copy in a work folder; a compiled map is checked
(``map check``) before it is copied into ``maps/``, under a new name.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.services.base import Cancelled, Reporter

TOOLS = ("hlcsg", "hlbsp", "hlvis", "hlrad", "bspguy", "decompiler", "trenchbroom")
COMPILERS = ("hlcsg", "hlbsp", "hlvis", "hlrad")
HINTS = {
    "hlcsg": "VHLT (github.com/seedee/SDHLT or the VHLT v34 binaries): build it with CMake "
             "or run the Windows .exe through Wine",
    "bspguy": "bspguy (github.com/wootguy/bspguy): Windows / Linux builds; on macOS build it "
              "or run the .exe through Wine",
    "decompiler": "a BSP -> MAP decompiler (bspsrc-style tools, ericw-tools bsputil "
                  "--decompile …): set its path and its arguments with {bsp} and {map}",
    "trenchbroom": "TrenchBroom (trenchbroom.github.io), native on Windows, macOS and Linux",
}
ERROR_LINE = re.compile(r"(error|exceeded max_map_\w+|fatal|leak)", re.IGNORECASE)


def config_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / \
            "valve-qc-merger"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "valve-qc-merger"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "valve-qc-merger"


def settings_path() -> Path:
    return config_dir() / "map-tools.json"


def load_settings() -> dict[str, dict[str, object]]:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) \
        else {}


def save_settings(settings: dict[str, dict[str, object]]) -> Path:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=1), encoding="utf-8")
    return path


@dataclass
class Tool:
    name: str
    path: Path | None = None
    prefix: list[str] = field(default_factory=list)  # wine …
    args: list[str] = field(default_factory=list)  # extra / template arguments

    @property
    def available(self) -> bool:
        return self.path is not None

    @property
    def hint(self) -> str:
        return HINTS.get(self.name, HINTS["hlcsg"] if self.name in COMPILERS else "")

    def command(self, *arguments: str) -> list[str]:
        if self.path is None:
            raise FileNotFoundError(f"{self.name} is not set up: {self.hint}")
        prefix = list(self.prefix)
        if not prefix and self.path.suffix.lower() == ".exe" and os.name != "nt":
            wine = shutil.which("wine") or shutil.which("wine64")
            prefix = [wine] if wine else []
        if sys.platform == "darwin" and self.path.suffix == ".app":
            return ["open", "-a", str(self.path), *arguments]
        return [*prefix, str(self.path), *arguments]


def find_tool(name: str, settings: dict[str, dict[str, object]] | None = None) -> Tool:
    """``name`` from the settings file, ``VQM_<NAME>`` or ``PATH``."""
    settings = load_settings() if settings is None else settings
    entry = settings.get(name, {})
    raw_prefix, raw_args = entry.get("prefix"), entry.get("args")
    prefix = [str(p) for p in raw_prefix] if isinstance(raw_prefix, list) else []
    args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
    candidates = [entry.get("path"), os.environ.get(f"VQM_{name.upper()}")]
    exe_names = [name, f"{name}.exe"]
    if name == "trenchbroom":
        exe_names = ["trenchbroom", "TrenchBroom", "TrenchBroom.exe"]
        if sys.platform == "darwin":
            candidates.append("/Applications/TrenchBroom.app")
    candidates += [shutil.which(n) for n in exe_names]
    for candidate in candidates:
        if candidate and Path(str(candidate)).exists():
            return Tool(name, Path(str(candidate)), prefix, args)
    return Tool(name, None, prefix, args)


def tool_help(tool: Tool, timeout: float = 10.0) -> str:
    """The tool's own usage text (``--help``, else no arguments)."""
    for arguments in (["--help"], []):
        try:
            done = subprocess.run(tool.command(*arguments), capture_output=True, text=True,
                                  errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
            continue
        text = (done.stdout or "") + (done.stderr or "")
        if text.strip():
            return text
    return ""


def options_in_help(text: str) -> list[str]:
    """The ``-option`` names a usage text lists."""
    return sorted(set(re.findall(r"(?m)^\s*(-{1,2}[A-Za-z][\w-]*)", text)))


@dataclass
class ToolRun:
    command: list[str]
    code: int = 0
    log: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # lines that read like errors

    @property
    def ok(self) -> bool:
        return self.code == 0


def run_tool(tool: Tool, arguments: list[str], cwd: Path, reporter: Reporter | None = None,
             timeout: float | None = None) -> ToolRun:
    """Run ``tool`` streaming its output to ``reporter``; cancellable."""
    reporter = reporter or Reporter()
    run = ToolRun(tool.command(*arguments))
    reporter.log("$ " + " ".join(run.command))
    process = subprocess.Popen(run.command, cwd=cwd, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                               errors="replace")
    assert process.stdout is not None
    try:
        for line in process.stdout:
            line = line.rstrip()
            run.log.append(line)
            reporter.log(f"  {line}")
            if ERROR_LINE.search(line):
                run.errors.append(line.strip())
            if reporter.cancelled():
                raise Cancelled("cancelled")
        run.code = process.wait(timeout=timeout)
    except (Cancelled, subprocess.TimeoutExpired):
        process.kill()
        process.wait()
        raise
    return run


@dataclass
class CompileResult:
    bsp: Path | None = None
    runs: list[ToolRun] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        return [e for run in self.runs for e in run.errors]


def compile_map(map_file: Path, *, steps: tuple[str, ...] = COMPILERS,
                options: dict[str, list[str]] | None = None, wads: list[Path] | None = None,
                reporter: Reporter | None = None,
                settings: dict[str, dict[str, object]] | None = None) -> CompileResult:
    """``.map`` -> ``.bsp`` with the compilers in ``steps`` (each
    ``<tool> [settings args] [options] <name>`` in a work folder holding a
    copy of the map and ``wads``). The BSP is left in the work folder."""
    result = CompileResult()
    work = Path(tempfile.mkdtemp(prefix="vqm_compile_"))
    shutil.copy2(map_file, work / map_file.name)
    for wad in wads or []:
        shutil.copy2(wad, work / wad.name)
    name = map_file.stem
    for step in steps:
        tool = find_tool(step, settings)
        if not tool.available:
            result.notes.append(f"{step} is not set up: {tool.hint}")
            return result
        run = run_tool(tool, [*tool.args, *(options or {}).get(step, []), name], work, reporter)
        result.runs.append(run)
        if not run.ok:
            result.notes.append(f"{step} failed (exit {run.code})")
            return result
    bsp = work / f"{name}.bsp"
    result.bsp = bsp if bsp.is_file() else None
    if result.bsp is None:
        result.notes.append("the compilers wrote no .bsp")
    return result


def decompile_map(bsp: Path, out: Path, reporter: Reporter | None = None,
                  settings: dict[str, dict[str, object]] | None = None) -> ToolRun:
    """Run the configured decompiler: its ``args`` template with ``{bsp}``
    and ``{map}`` (default ``{bsp} {map}``)."""
    tool = find_tool("decompiler", settings)
    template = tool.args or ["{bsp}", "{map}"]
    arguments = [a.replace("{bsp}", str(bsp)).replace("{map}", str(out)) for a in template]
    keep_args, tool.args = tool.args, []
    try:
        return run_tool(tool, arguments, out.parent, reporter)
    finally:
        tool.args = keep_args


def open_in_editor(map_file: Path, settings: dict[str, dict[str, object]] | None = None
                   ) -> subprocess.Popen[bytes]:
    """Open a ``.map`` in TrenchBroom (detached)."""
    tool = find_tool("trenchbroom", settings)
    return subprocess.Popen(tool.command(str(map_file)), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# -- editing a decompiled .map ---------------------------------------------------

_VALVE_FACE = re.compile(
    r"^(\s*\([^)]*\)\s*\([^)]*\)\s*\([^)]*\)\s*)(\S+)"
    r"(\s*\[\s*)(\S+\s+\S+\s+\S+)(\s+)(\S+)(\s*\]\s*\[\s*)(\S+\s+\S+\s+\S+)(\s+)(\S+)"
    r"(\s*\]\s*)(\S+)(\s+)(\S+)(\s+)(\S+)(.*)$", re.MULTILINE)


def rescale_textures(map_text: str, factors: dict[str, float]) -> tuple[str, int]:
    """A Valve 220 ``.map`` with textures shrunk by ``factors`` (old size /
    new size, by texture name) kept the same size on the walls: their scale
    multiplied, their offsets (texels) divided. Returns (text, faces changed)."""
    wanted = {k.lower(): v for k, v in factors.items()}
    changed = 0

    def edit(match: re.Match[str]) -> str:
        nonlocal changed
        factor = wanted.get(match.group(2).lower())
        if not factor:
            return match.group(0)
        changed += 1
        g = list(match.groups())
        g[5] = f"{float(g[5]) / factor:g}"  # u offset
        g[9] = f"{float(g[9]) / factor:g}"  # v offset
        g[13] = f"{float(g[13]) * factor:g}"  # u scale
        g[15] = f"{float(g[15]) * factor:g}"  # v scale
        return "".join(g)

    return _VALVE_FACE.sub(edit, map_text), changed


def parse_errors(run: ToolRun) -> list[dict[str, str]]:
    """A run's error lines as check-report issues."""
    out = []
    for line in run.errors:
        code = "compile_limit" if "exceeded" in line.lower() else \
            "compile_leak" if "leak" in line.lower() else "compile_error"
        out.append({"level": "critical", "code": code, "text": line})
    return out




# -- scenarios ------------------------------------------------------------------

@dataclass
class ShrinkResult:
    map_file: Path | None = None  # the decompiled, rescaled .map
    wad: Path | None = None  # the shrunk textures
    bsp: Path | None = None  # compiled (in its work folder)
    shrunk: dict[str, tuple[int, int]] = field(default_factory=dict)  # name -> new size
    runs: list[ToolRun] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def install_map(bsp: Path, game: Path, name: str) -> Path:
    """Copy a compiled BSP into ``game/maps`` as ``name`` — or the next
    free ``_sp`` name when one is there (a map players have is never
    overwritten)."""
    from valve_qc_merger.mapkit.fix import next_name
    maps = game / "maps"
    maps.mkdir(parents=True, exist_ok=True)
    target = name if not (maps / f"{name}.bsp").exists() else next_name(maps, name)
    dest = maps / f"{target}.bsp"
    shutil.copy2(bsp, dest)
    return dest


def shrink_textures(bsp_path: Path, size: int = 256, names: list[str] | None = None, *,
                    folders: list[Path] | None = None, reporter: Reporter | None = None,
                    settings: dict[str, dict[str, object]] | None = None) -> ShrinkResult:
    """Textures over ``size`` made smaller without changing how big they
    look: decompile the map, write the textures halved (until they fit) to
    ``<map>_small.wad``, multiply their scale in the ``.map`` by the factor,
    list the WAD first, recompile. The BSP stays in the work folder:
    :func:`install_map` copies it after a check."""
    from valve_qc_merger.mapkit import bsp as B
    from valve_qc_merger.mapkit.analyze import map_wads
    from valve_qc_merger.mapkit.geometry import geometry
    from valve_qc_merger.mapkit.textures import lump_entries, texture_from_image
    from valve_qc_merger.sprite.wad import MIPTEX, _resize, read_miptex, write_wad

    result = ShrinkResult()
    bsp = B.Bsp.read(bsp_path)
    wads = map_wads(bsp_path, bsp, folders)
    entries = lump_entries(bsp)
    wanted = {n.lower() for n in names} if names else None
    small = []
    factors: dict[str, float] = {}
    for tex in geometry(bsp).textures:
        if tex.offset < 0 or max(tex.width, tex.height) <= size:
            continue
        if wanted is not None and tex.name.lower() not in wanted:
            continue
        raw = entries[tex.index] if tex.embedded else next(
            (lump for w in wads if (lump := w.lump(tex.name)) is not None), None)
        if raw is None:
            result.notes.append(f"{tex.name}: pixels not found (its WAD is missing)")
            continue
        texture = read_miptex(raw, tex.name, MIPTEX)
        factor = 1
        while max(tex.width, tex.height) // factor > size:
            factor *= 2
        width, height = max(tex.width // factor, 16), max(tex.height // factor, 16)
        rgba = _resize(texture.rgba(), width, height)
        small.append(texture_from_image(tex.name, rgba, width, height))
        factors[tex.name] = float(factor)
        result.shrunk[tex.name] = (width, height)
    if not small:
        result.notes.append(f"no texture over {size}")
        return result
    work = Path(tempfile.mkdtemp(prefix="vqm_shrink_"))
    result.wad = work / f"{bsp_path.stem}_small.wad"
    result.wad.write_bytes(write_wad(small))
    map_file = work / f"{bsp_path.stem}.map"
    run = decompile_map(bsp_path, map_file, reporter, settings)
    result.runs.append(run)
    if not run.ok or not map_file.is_file():
        result.notes.append("the decompiler wrote no .map"
                            + (f" (exit {run.code})" if run.code else ""))
        return result
    text, changed = rescale_textures(map_file.read_text(encoding="latin-1"), factors)
    text = _prepend_wad(text, result.wad.name)
    map_file.write_text(text, encoding="latin-1")
    result.map_file = map_file
    result.notes.append(f"{changed} faces rescaled")
    compiled = compile_map(map_file, wads=[result.wad, *(w.path for w in wads)],
                           reporter=reporter, settings=settings)
    result.runs += compiled.runs
    result.notes += compiled.notes
    result.bsp = compiled.bsp
    return result


def _prepend_wad(map_text: str, wad: str) -> str:
    match = re.search(r'"wad"\s*"([^"]*)"', map_text)
    if match is None:
        return re.sub(r"\{", '{\n"wad" "' + wad + '"', map_text, count=1)
    return map_text[:match.start(1)] + wad + ";" + match.group(1) + map_text[match.end(1):]


__all__ = ["COMPILERS", "CompileResult", "HINTS", "TOOLS", "Tool", "ToolRun", "compile_map",
           "config_dir", "decompile_map", "find_tool", "load_settings", "open_in_editor",
           "options_in_help", "parse_errors", "rescale_textures", "run_tool", "save_settings",
           "settings_path", "shrink_textures", "tool_help", "install_map", "ShrinkResult"]
