"""The limits merges split their parts by — set per run from the compiler
and the server, not fixed in each merge.

- **Submodels per model** (blanks included): stock HLSDK studiomdl keeps 32 in
  ONE array for the whole model and writes past it silently (meshes detach
  from their bones in game); the engine and ReHLDS read the count from the
  file and have no such cap. Our studiomdl (``tools/build_studiomdl.py``)
  keeps 1024 and prints ``MAXSTUDIOMODELS N`` — :func:`studiomdl_submodels`
  reads it; any other compiler counts as stock (32).
- **pev_body values**: a view model's body goes out in ONE byte
  (``SVC_WEAPONANIM``): 256 values, whatever the server. Every other entity's
  body (players and so their p_ model, w_, props) is sent with the bits the
  server's ``delta.lst`` gives it (ReGameDLL: 9 for players, 18 for
  entities; the field holds up to 32) — merges assume 32 bits; the Server
  window says what the server's delta.lst allows.
- **File size**: a compiled ``.mdl`` (and its ``T.mdl``) stays under 16 MB —
  bigger files risk the client; our studiomdl refuses them. Merges close a
  part when its textures and meshes (:func:`texture_bytes`, :func:`mesh_bytes`)
  near :data:`PART_BYTES`.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

STOCK_SUBMODELS = 32
VIEW_BODY_VALUES = 256  # SVC_WEAPONANIM writes body as a byte
DEFAULT_BODY_BITS = 32  # delta.lst's DT_INTEGER field holds up to 32
MAX_MDL_BYTES = 16 * 1024 * 1024
# a part's textures + meshes (estimated) stay under this: room for the
# animations and headers below 16 MB
PART_BYTES = int(14.5 * 1024 * 1024)
PART_TEXTURE_BYTES = PART_BYTES  # (textures alone: the same ceiling)

_state: dict[str, int] = {"submodels": STOCK_SUBMODELS, "body_bits": DEFAULT_BODY_BITS}
_BANNER = re.compile(rb"MAXSTUDIOMODELS\s+(\d+)")
_cache: dict[tuple[str, int, float], int] = {}


def submodels() -> int:
    """Submodels one model may hold, with the compiler of this run."""
    return _state["submodels"]


def body_values() -> int:
    """pev_body values a non-view entity can carry (2 ** bits)."""
    return 2 ** _state["body_bits"]


@contextmanager
def use(*, submodels: int | None = None, body_bits: int | None = None) -> Iterator[None]:
    """Run merges under these limits (restored afterwards)."""
    saved = dict(_state)
    if submodels is not None:
        _state["submodels"] = submodels
    if body_bits is not None:
        _state["body_bits"] = body_bits
    try:
        yield
    finally:
        _state.update(saved)


def studiomdl_submodels(studiomdl: str | Path | None) -> int:
    """MAXSTUDIOMODELS of a studiomdl: what our build prints when run
    without arguments; :data:`STOCK_SUBMODELS` for any other compiler (or
    none)."""
    if not studiomdl:
        return STOCK_SUBMODELS
    path = Path(studiomdl)
    try:
        stat = path.stat()
    except OSError:
        return STOCK_SUBMODELS
    key = (str(path.resolve()), stat.st_size, stat.st_mtime)
    if key not in _cache:
        try:
            run = subprocess.run([str(path)], capture_output=True, timeout=10,
                                 stdin=subprocess.DEVNULL)
            match = _BANNER.search(run.stdout + run.stderr)
            _cache[key] = int(match.group(1)) if match else STOCK_SUBMODELS
        except (OSError, subprocess.SubprocessError):
            _cache[key] = STOCK_SUBMODELS
    return _cache[key]


def mesh_bytes(smds: object) -> int:
    """What reference meshes add to a compiled .mdl, estimated: 13 bytes a
    vertex and a normal (vector + bone), 11 a triangle (strips/fans) —
    within 1-3 % of studiomdl on a 50-body players part."""
    total = 0
    for smd in smds:  # type: ignore[attr-defined]
        triangles = smd.triangles
        verts = {(v.bone, v.position) for t in triangles for v in t.vertices}
        normals = {(v.bone, v.normal, t.material.lower()) for t in triangles
                   for v in t.vertices}
        total += 13 * (len(verts) + len(normals)) + 11 * len(triangles)
    return total


def texture_bytes(path: Path) -> int:
    """What an 8-bit BMP texture costs inside a .mdl: its pixels and palette."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(26)
        width, height = int.from_bytes(head[18:22], "little", signed=True), \
            int.from_bytes(head[22:26], "little", signed=True)
        return abs(width) * abs(height) + 768
    except OSError:
        return 0


__all__ = ["DEFAULT_BODY_BITS", "MAX_MDL_BYTES", "PART_BYTES", "PART_TEXTURE_BYTES",
           "STOCK_SUBMODELS", "VIEW_BODY_VALUES", "body_values", "mesh_bytes",
           "studiomdl_submodels", "submodels", "texture_bytes", "use"]
