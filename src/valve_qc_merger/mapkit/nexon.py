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
import re
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


def portable_path(rel: str) -> str:
    """A game path in lower case, each name ``[a-z0-9_]`` (the extension kept):
    ``models/Props/apc_back-arctic.mdl`` -> ``models/props/apc_back_arctic.mdl``."""
    parts = rel.replace("\\", "/").lower().split("/")
    out = []
    for k, part in enumerate(parts):
        stem, dot, ext = part.rpartition(".") if k == len(parts) - 1 and "." in part \
            else (part, "", "")
        out.append(re.sub(r"[^a-z0-9_]+", "_", stem).strip("_") + dot + ext or part)
    return "/".join(out)


def _companions(path: Path, rel: str, target: str) -> list[tuple[str, str, str]]:
    """(where to find it, kind, where to write it) of a model's companions:
    ``<model>T.mdl`` (the engine asks for a capital T), ``<model>01.mdl`` …
    — named after the model as written — and the sounds its events play
    (named as the events name them)."""
    refs = model_refs(path)
    out: list[tuple[str, str, str]] = []
    stem, new = rel[:-4], target[:-4]
    if refs.needs_texture_file:
        out.append((f"{stem}t.mdl", "companion", f"{new}T.mdl"))
    out += [(f"{stem}{n:02d}.mdl", "companion", f"{new}{n:02d}.mdl")
            for n in range(1, refs.sequence_groups)]
    out += [(f"sound/{s.removeprefix('sound/')}", "event_sound",
             f"sound/{s.removeprefix('sound/')}".lower()) for s in refs.sounds]
    return out


SKY_MAX = 256
TEXTURE_MAX = 512  # a model's skin larger than this: some clients refuse it


def _tga24(rgba: object) -> bytes:
    """An uncompressed 24-bit top-left TGA of an (h, w, 4) array."""
    import numpy as np
    image = np.asarray(rgba)
    height, width = image.shape[:2]
    head = struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, width, height, 24, 0x20)
    return head + image[..., [2, 1, 0]].astype(np.uint8).tobytes()


def _shrink_sky(data: bytes) -> bytes | None:
    from valve_qc_merger.mapkit.images import ImageError, read_image
    from valve_qc_merger.sprite.wad import _resize
    try:
        rgba = read_image(data)
    except (ImageError, ValueError, struct.error):
        return None
    height, width = rgba.shape[:2]
    scale = SKY_MAX / max(width, height)
    return _tga24(_resize(rgba, max(int(width * scale), 1), max(int(height * scale), 1)))


def _convert(kind: str, rel: str, data: bytes, shrink_sky: bool = False
             ) -> tuple[bytes, list[str], list[str]]:
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
        from valve_qc_merger.limits import MAX_MDL_BYTES
        if len(data) > MAX_MDL_BYTES:
            left.append(f"{len(data) / 1e6:.1f} MB: over the engine's 16 MB for a model")
        _check_skins(data, left)
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
        if max(width, height) > SKY_MAX:
            smaller = _shrink_sky(data) if shrink_sky else None
            if smaller is not None:
                data = smaller
                done.append(f"sky side {width}x{height} -> at most {SKY_MAX}")
            else:
                left.append(f"sky side {width}x{height}: over {SKY_MAX} (some clients scale it "
                            "down; --shrink-sky makes it smaller)")
    return data, done, left


def _check_skins(data: bytes, left: list[str]) -> None:
    """Skins over 512 a side (the model's own textures)."""
    try:
        numtextures, textureindex = struct.unpack_from("<2i", data, 180)
        for i in range(min(max(numtextures, 0), 256)):
            off = textureindex + i * 80
            name = data[off:off + 64].split(b"\0", 1)[0].decode("latin-1")
            _flags, width, height = struct.unpack_from("<3i", data, off + 64)
            if max(width, height) > TEXTURE_MAX:
                left.append(f"skin {name} {width}x{height}: over {TEXTURE_MAX}")
    except struct.error:
        left.append("truncated model")


def import_map(map_name: str, index: SourceIndex, out: Path, *,
               base: list[Path] | None = None, overwrite: bool = False,
               result: ImportResult | None = None, shrink_sky: bool = False) -> ImportResult:
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
    queue: list[tuple[str, ...]] = list(_needs(index, bsp, source_map.stem))
    world = next((e for e in parse(bsp.entity_text) if e.get("classname") == "worldspawn"), {})
    sky = world.get("skyname", "").strip()
    new_sky = sky
    if sky and stock is not None:
        # one side the base game has with other pixels: the whole sky gets a name of its own
        for rel_side, kind_side in queue:
            if kind_side != "sky":
                continue
            have, _exact = stock.find(rel_side)
            mine = index.find(rel_side)
            if have is not None and mine is not None and _sha1(have) != _sha1(mine):
                new_sky = f"{sky}_cso"
                break
    wad_renames: dict[str, str] = {}
    seen: set[str] = set()
    forced: dict[str, str] = {}  # companions: named after their model as written
    while queue:
        item = queue.pop(0)
        rel, kind = item[0], item[1]
        if len(item) > 2:
            forced[rel.lower()] = str(item[2])
        rel = rel.replace("\\", "/").lstrip("/")
        key = rel.lower()
        if key in seen:
            continue
        seen.add(key)
        found = index.find_named(rel, ".wad") if kind == "wad" else index.find(rel)
        if kind == "wad":
            target_rel = rel
        elif key in forced:
            target_rel = forced[key]
        elif kind in ("model", "sound"):
            target_rel = portable_path(rel)
            if target_rel != key:
                result.renames[rel] = target_rel
        else:
            target_rel = key
        if kind == "sky" and new_sky != sky:
            target_rel = f"gfx/env/{new_sky.lower()}{key.rsplit(sky.lower(), 1)[-1]}"
        record = result.files.get(key) or ImportedFile(target_rel)
        result.files[key] = record
        if name not in record.maps:
            record.maps.append(name)
        if kind == "sky" and new_sky != sky:
            record.path, record.state = target_rel, "renamed"
        elif stock is not None:
            have, _exact = stock.find(rel)
            if have is not None:
                if found is None or _sha1(have) == _sha1(found):
                    record.state = "stock"
                    continue
                stem, dot, ext = target_rel.rpartition(".")
                target_rel = f"{stem}_cso{dot}{ext}"
                if kind == "companion" and stem.endswith("T"):
                    target_rel = f"{stem[:-1]}_csoT{dot}{ext}"
                record.path = target_rel
                record.state = "renamed"
                if kind in ("model", "sound"):
                    result.renames[rel] = target_rel
                elif kind == "wad":
                    wad_renames[rel.lower()] = target_rel
                else:
                    result.notes.append(f"{rel}: the base game has another one — the map "
                                        "uses the base game's (a renamed copy is written)")
        if found is None:
            record.state = "missing"
            continue
        record.source = str(found)
        data, done, left = _convert(kind, target_rel, found.read_bytes(), shrink_sky)
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
        if key.endswith(".mdl") and kind != "companion":
            queue += _companions(found, key, target_rel)
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
    if wad_renames:
        names = [wad_renames.get(n.lower(), n) for n in wad_names(world.get("wad", ""))]
        plan.add("worldspawn.wad", f"wad: {', '.join(wad_renames.values())} (the base game's "
                 "WADs of those names differ)", None, value=";".join(names) + ";")
    if new_sky != sky:
        index_world = next(i for i, e in enumerate(entities) if e.get("classname") == "worldspawn")
        plan.add("entity.set", f"skyname: {sky} -> {new_sky} (the base game's sky of that name "
                 "differs)", index_world, key="skyname", value=new_sky)
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
           "portable_path", "write_record"]
