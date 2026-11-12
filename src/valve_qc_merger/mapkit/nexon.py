"""Import a map with everything it needs from unpacked CSO / Nexon files.

The source is a folder already unpacked (this tool never opens the
client's archives): a client's ``cstrike`` (or the folder above it), a map
pack, or several of them, the first one winning. :class:`SourceIndex` lists
their files by lower-case game path (cached in the user's cache folder —
never in the repository: CSO data is not distributed, not even as a list).

:func:`import_map` takes the map's closure —

- worldspawn ``wad`` (WADs found by file name anywhere in the source),
  ``skyname`` (six ``gfx/env`` TGAs), ``maps/<map>_detail.txt`` and its
  detail textures, ``overviews/<map>.txt`` and its picture, ``maps/<map>.txt``;
- every model, sprite and sound an entity names, and for a model its
  ``<name>t.mdl`` / ``<name>01.mdl`` companions (read from its header) and
  the sounds its animation events play —

and copies it into a game folder in Counter-Strike's layout:

- a file the base game already has (``--game``) with the same content is
  not copied; with other content it is copied as ``<name>_cso.<ext>`` and the
  entities point at that (else clients would see the stock one);
- names are put in lower case (Linux servers and FastDL hosts are case
  sensitive) and the entities rewritten to match; WAD names stay as the map
  lists them;
- version 9 models (CSO) have version 10's layout: the version is patched
  and the model read back; sounds the engine does not play (stereo, 24 bit,
  odd rates) are converted to 16-bit mono PCM;
- a file the game folder already has with other content is kept unless
  ``overwrite``.

``import.json`` records each file's source, SHA-1, conversions and the maps
needing it; the map is checked afterwards.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path

from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.analyze import Finder, entity_resources, finder_for
from valve_qc_merger.mapkit.fix import FixPlan, save_plan
from valve_qc_merger.server.bsp import wad_names
from valve_qc_merger.server.entities import parse
from valve_qc_merger.server.maps import SKY_SIDES, _detail_textures, _overview
from valve_qc_merger.server.scan import model_refs

GAME_SUBFOLDERS = ("cstrike", "czero", "valve")


def cache_dir() -> Path:
    """The user's cache folder for this tool (never the repository)."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "valve-qc-merger"


@dataclass
class SourceIndex:
    roots: list[Path]
    files: dict[str, Path] = field(default_factory=dict)  # lower game path -> file
    names: dict[str, list[str]] = field(default_factory=dict)  # lower file name -> paths

    @staticmethod
    def game_roots(root: Path) -> list[Path]:
        """A client folder -> its ``cstrike`` (then ``czero``, ``valve``);
        anything else is a game folder itself."""
        subs = [root / s for s in GAME_SUBFOLDERS if (root / s).is_dir()]
        return subs or [root]

    @classmethod
    def build(cls, roots: list[Path], cache: bool = True) -> SourceIndex:
        index = cls([r.resolve() for r in roots])
        for root in roots:
            for game in cls.game_roots(Path(root).resolve()):
                for rel in _listing(game, cache):
                    key = rel.lower()
                    if key not in index.files:
                        index.files[key] = game / rel
                        index.names.setdefault(key.rsplit("/", 1)[-1], []).append(key)
        return index

    def find(self, relative: str) -> Path | None:
        return self.files.get(relative.replace("\\", "/").lstrip("/").lower())

    def find_named(self, name: str, suffix: str = "") -> Path | None:
        """A file by its name alone (WADs live anywhere in a CSO dump),
        preferring the shortest path."""
        keys = self.names.get(name.lower(), [])
        keys = [k for k in keys if k.endswith(suffix)] if suffix else keys
        return self.files[min(keys, key=len)] if keys else None


def _listing(game: Path, cache: bool) -> list[str]:
    """Every file under ``game`` (relative POSIX paths), cached by the
    folder's path and the modification times of it and its children."""
    stamp = [str(game), game.stat().st_mtime]
    try:
        stamp += sorted((p.name, p.stat().st_mtime) for p in game.iterdir())
    except OSError:
        pass
    key = hashlib.sha1(json.dumps(stamp, default=str).encode()).hexdigest()[:16]
    path = cache_dir() / "source-index" / f"{key}.json"
    if cache and path.is_file():
        try:
            return list(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    out = sorted(p.relative_to(game).as_posix() for p in game.rglob("*") if p.is_file())
    if cache:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(out), encoding="utf-8")
        except OSError:
            pass
    return out


@dataclass
class ImportedFile:
    path: str  # where it went in the game folder
    source: str = ""
    sha1: str = ""
    conversions: list[str] = field(default_factory=list)
    maps: list[str] = field(default_factory=list)
    state: str = "copied"  # copied | stock | renamed | kept | missing | same


@dataclass
class ImportResult:
    maps: list[str] = field(default_factory=list)
    files: dict[str, ImportedFile] = field(default_factory=dict)
    renames: dict[str, str] = field(default_factory=dict)  # entity path -> new path
    notes: list[str] = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        return [f.path for f in self.files.values() if f.state == "missing"]


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _needs(index: SourceIndex, bsp: B.Bsp, name: str) -> list[tuple[str, str]]:
    """(game path, kind) a map needs (see the module)."""
    entities = parse(bsp.entity_text)
    world = next((e for e in entities if e.get("classname") == "worldspawn"), {})
    out: list[tuple[str, str]] = [(w, "wad") for w in wad_names(world.get("wad", ""))]
    sky = world.get("skyname", "").strip()
    if sky:
        out += [(f"gfx/env/{sky}{side}.tga", "sky") for side in SKY_SIDES]
    for rel, kind in ((f"maps/{name}.txt", "info"), (f"maps/{name}_detail.txt", "detail"),
                      (f"overviews/{name}.txt", "overview")):
        found = index.find(rel)
        if found is None:
            continue
        out.append((rel, kind))
        text = found.read_text(encoding="latin-1")
        if kind == "detail":
            out += [(t, "detail") for t in _detail_textures(text)]
        elif kind == "overview":
            out += [(t, "overview") for t in _overview(text)]
    models, sounds = entity_resources(entities)
    out += [(m, "model") for m in models]
    out += [(f"sound/{s}", "sound") for s in sounds]
    return out


def _companions(path: Path, rel: str) -> list[tuple[str, str]]:
    refs = model_refs(path)
    out: list[tuple[str, str]] = []
    stem = rel[:-4]
    if refs.needs_texture_file:
        out.append((f"{stem}t.mdl", "model"))
    out += [(f"{stem}{n:02d}.mdl", "model") for n in range(1, refs.sequence_groups)]
    out += [(f"sound/{s.removeprefix('sound/')}", "sound") for s in refs.sounds]
    return out


def _convert(kind: str, rel: str, data: bytes) -> tuple[bytes, list[str], list[str]]:
    """(bytes to write, conversions done, problems left)."""
    done: list[str] = []
    left: list[str] = []
    low = rel.lower()
    if low.endswith(".mdl") and data[:4] == b"IDST":
        (version,) = struct.unpack_from("<i", data, 4)
        if version == 9:
            patched = data[:4] + struct.pack("<i", 10) + data[8:]
            import tempfile
            with tempfile.TemporaryDirectory(prefix="vqm_mdl_") as tmp:
                probe = Path(tmp) / "probe.mdl"
                probe.write_bytes(patched)
                refs = model_refs(probe)
            if refs.error:
                left.append(f"version 9 and not readable as 10 ({refs.error}): recompile it")
            else:
                data = patched
                done.append("studio version 9 -> 10")
        elif version != 10:
            left.append(f"studio version {version}: CS 1.6 loads only 10")
    elif low.endswith(".wav"):
        from valve_qc_merger.sound.wav import WavError, fix, problems, read_wav
        try:
            wav = read_wav(data)
            issues = [p for p in problems(wav) if not p.startswith(("silent", "clipped"))]
            if issues:
                data = fix(wav)
                done.append("sound -> 16-bit mono PCM (" + "; ".join(issues) + ")")
        except (WavError, ValueError, struct.error) as exc:
            left.append(f"not a WAV the engine plays: {exc}")
    elif low.endswith(".spr") and data[:4] == b"IDSP":
        (version,) = struct.unpack_from("<i", data, 4)
        if version != 2:
            left.append(f"sprite version {version}: CS 1.6 loads only 2")
    elif kind == "sky" and low.endswith(".tga") and len(data) >= 18:
        width, height = struct.unpack_from("<HH", data, 12)
        if max(width, height) > 256:
            left.append(f"sky side {width}x{height}: over 256 (some clients scale it down)")
    return data, done, left


def import_map(map_name: str, index: SourceIndex, out: Path, *,
               base: list[Path] | None = None, overwrite: bool = False,
               result: ImportResult | None = None) -> ImportResult:
    """Import ``map_name`` (a name in the source, or a .bsp path) into game
    folder ``out`` (see the module)."""
    result = result or ImportResult()
    source_map = Path(map_name) if map_name.lower().endswith(".bsp") and Path(map_name).is_file() \
        else index.find(f"maps/{map_name}.bsp")
    if source_map is None:
        result.notes.append(f"{map_name}: no maps/{map_name}.bsp in the source")
        return result
    name = source_map.stem.lower()
    bsp = B.Bsp.read(source_map)
    stock: Finder | None = finder_for([b.resolve() for b in base]) if base else None
    out = Path(out)
    queue = _needs(index, bsp, source_map.stem)
    seen: set[str] = set()
    while queue:
        rel, kind = queue.pop(0)
        rel = rel.replace("\\", "/").lstrip("/")
        key = rel.lower()
        if key in seen:
            continue
        seen.add(key)
        found = index.find_named(rel, ".wad") if kind == "wad" else index.find(rel)
        target_rel = rel if kind == "wad" else key
        record = result.files.get(key) or ImportedFile(target_rel)
        result.files[key] = record
        if name not in record.maps:
            record.maps.append(name)
        if stock is not None:
            have, _exact = stock.find(rel)
            if have is not None:
                if found is None or _sha1(have) == _sha1(found):
                    record.state = "stock"
                    continue
                stem, dot, ext = target_rel.rpartition(".")
                target_rel = f"{stem}_cso{dot}{ext}"
                record.path = target_rel
                record.state = "renamed"
                if kind in ("model", "sound"):
                    result.renames[rel] = target_rel
                else:
                    result.notes.append(f"{rel}: the base game has another one — the map "
                                        "uses the base game's (a renamed copy is written)")
        if found is None:
            record.state = "missing"
            continue
        record.source = str(found)
        data, done, left = _convert(kind, target_rel, found.read_bytes())
        record.conversions = done
        result.notes += [f"{target_rel}: {p}" for p in left]
        record.sha1 = hashlib.sha1(data).hexdigest()
        dest = out / target_rel
        if dest.exists() and not overwrite:
            if hashlib.sha1(dest.read_bytes()).hexdigest() != record.sha1:
                record.state = "kept"
                result.notes.append(f"{target_rel}: the game folder has another one (kept; "
                                    "--overwrite replaces it)")
            elif record.state == "copied":
                record.state = "same"
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        if key.endswith(".mdl"):
            queue += _companions(found, key)
    from valve_qc_merger.mapkit.analyze import forget
    forget(out)
    # the map itself, entities pointing at the names written
    maps = out / "maps"
    maps.mkdir(parents=True, exist_ok=True)
    target = maps / f"{name}.bsp"
    if not target.exists() or overwrite:
        shutil.copy2(source_map, target)
    plan = FixPlan(target)
    entities = parse(bsp.entity_text)
    for index_, entity in enumerate(entities):
        for key, value in entity.items():
            plain = value.replace("\\", "/")
            if not plain.lower().endswith((".mdl", ".spr", ".wav")) or key == "wad":
                continue
            sound = plain.lower().endswith(".wav") and not plain.lower().startswith("sound/")
            rel = f"sound/{plain}" if sound else plain
            new = result.renames.get(rel, rel.lower())
            new = new.removeprefix("sound/") if sound else new
            if new != value:
                plan.add("entity.set", f"#{index_} {key}: {value} -> {new}", index_, key=key,
                         value=new)
    save_plan(plan, folders=base, backup=False)
    if name not in result.maps:
        result.maps.append(name)
    return result


def write_record(result: ImportResult, out: Path) -> Path:
    path = Path(out) / "import.json"
    old: dict[str, object] = {}
    if path.is_file():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            old = {}
    old_files = old.get("files")
    files: dict[str, object] = dict(old_files) if isinstance(old_files, dict) else {}
    files.update({k: asdict(v) for k, v in result.files.items()})
    old_maps = old.get("maps")
    known = {str(m) for m in old_maps} if isinstance(old_maps, list) else set()
    maps = sorted(known | set(result.maps))
    path.write_text(json.dumps({"maps": maps, "files": files, "notes": result.notes},
                               indent=1), encoding="utf-8")
    return path


__all__ = ["ImportResult", "ImportedFile", "SourceIndex", "cache_dir", "import_map",
           "write_record"]
