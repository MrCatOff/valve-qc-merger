"""What a custom map needs on the client, its ``.res`` file, the mapcycle.

The engine sends a joining client the files the server precaches (models,
sounds) — but not the texture WADs a map lists, its sky, its detail
textures or its overview. Those go in ``maps/<map>.res`` (one path per
line); a map without one shows missing textures and a black sky to anyone
who downloaded only the BSP. :func:`map_needs` reads them out of the BSP:

- ``wad`` of worldspawn: the WADs (looked up by name at the mod's top);
- ``skyname``: ``gfx/env/<sky>{up,dn,lf,rt,ft,bk}.tga``;
- ``maps/<map>_detail.txt``: the detail textures (``gfx/<name>.tga``);
- ``overviews/<map>.txt`` and the image it names (spectator overview);
- entity models, sprites and sounds (precached — only checked).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.server.bsp import read_map_resources, wad_names
from valve_qc_merger.server.entities import read_entities
from valve_qc_merger.server.scan import FolderInventory

SKY_SIDES = ("up", "dn", "lf", "rt", "ft", "bk")


@dataclass
class MapNeeds:
    name: str
    res: list[str] = field(default_factory=list)  # what belongs in maps/<map>.res
    precached: list[str] = field(default_factory=list)  # entity files the engine sends
    model_slots: int = 0
    entities: int = 0


@dataclass
class MapCheck:
    needs: MapNeeds
    missing: list[str] = field(default_factory=list)  # needed, in no folder
    stock: list[str] = field(default_factory=list)  # only in the base game (valve)
    res_state: str = "none"  # none | ok | outdated
    res_extra: list[str] = field(default_factory=list)  # .res lines naming no file
    res_listed: list[str] = field(default_factory=list)  # the .res as it is
    download: int = 0  # bytes a player without the map downloads (BSP + its files)

    @property
    def to_write(self) -> list[str]:
        """The ``.res`` lines: needed files the server has (not missing, not
        base-game files every client has)."""
        skip = {p.lower() for p in self.missing + self.stock}
        return [p for p in self.needs.res if p.lower() not in skip]


def _detail_textures(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        parts = line.split("//", 1)[0].split()
        if len(parts) >= 2:
            out.append(f"gfx/{parts[1].replace(chr(92), '/')}.tga")
    return out


def _overview(text: str) -> list[str]:
    match = re.search(r'IMAGE\s+"?([^"\s]+)"?', text, re.IGNORECASE)
    return [match.group(1).replace("\\", "/")] if match else []


def map_needs(path: Path, game_dir: Path | None = None) -> MapNeeds:
    """Read the needs of map ``path``; ``game_dir`` (default: the folder
    above ``maps/``) holds its detail and overview files."""
    path = Path(path)
    game_dir = Path(game_dir) if game_dir else path.parent.parent
    resources = read_map_resources(path)
    needs = MapNeeds(path.stem, model_slots=resources.model_slots,
                     entities=resources.entities)
    world = next((e for e in read_entities(path).entities
                  if e.get("classname") == "worldspawn"), {})
    res = list(wad_names(world.get("wad", "")))
    sky = world.get("skyname", "").strip()
    if sky:
        res += [f"gfx/env/{sky}{side}.tga" for side in SKY_SIDES]
    detail = game_dir / "maps" / f"{path.stem}_detail.txt"
    if detail.is_file():
        res.append(f"maps/{detail.name}")
        res += _detail_textures(detail.read_text(encoding="latin-1"))
    overview = game_dir / "overviews" / f"{path.stem}.txt"
    if overview.is_file():
        res.append(f"overviews/{overview.name}")
        res += _overview(overview.read_text(encoding="latin-1"))
    seen: set[str] = set()
    needs.res = [p for p in res if not (p.lower() in seen or seen.add(p.lower()))]
    needs.precached = sorted(resources.models) + sorted(f"sound/{s}" for s in
                                                       resources.sounds)
    return needs


def parse_res(text: str) -> list[str]:
    """The paths of a ``.res`` file (``//`` comments, quotes allowed)."""
    out = []
    for line in text.splitlines():
        line = line.split("//", 1)[0].strip().strip('"').replace("\\", "/")
        if line:
            out.append(line)
    return out


def res_text(lines: list[str], map_name: str) -> str:
    return (f"// {map_name}.res - files clients download with the map "
            "(written by valve-qc-merger Studio)\n" + "\n".join(lines) + "\n")


def check_map(path: Path, inventory: FolderInventory,
              fallback: list[FolderInventory] | None = None) -> MapCheck:
    """``map_needs`` against what the folders hold. ``fallback`` lists the
    downloads folder and the base game; a file only the base game (``valve``)
    has is stock — every client has it."""
    fallback = fallback or []
    needs = map_needs(path, inventory.root)
    result = MapCheck(needs)
    for wanted in needs.res + needs.precached:
        if inventory.exists(wanted):
            continue
        holders = [f for f in fallback if f.exists(wanted)]
        if not holders:
            result.missing.append(wanted)
        elif all(f.root.name.lower() == "valve" for f in holders):
            result.stock.append(wanted)
    sizes = {}
    for folder in [inventory, *(f for f in fallback if f.root.name.lower() != "valve")]:
        for wanted in needs.res + needs.precached:
            actual = folder.actual(wanted)
            if actual is not None and wanted.lower() not in sizes:
                sizes[wanted.lower()] = folder.files[actual]
    result.download = Path(path).stat().st_size + sum(sizes.values())
    res_file = Path(path).with_suffix(".res")
    if res_file.is_file():
        listed = parse_res(res_file.read_text(encoding="latin-1"))
        result.res_listed = listed
        lower = {p.lower() for p in listed}
        result.res_extra = [p for p in listed if not inventory.exists(p)
                            and not any(f.exists(p) for f in fallback)]
        complete = all(p.lower() in lower for p in result.to_write)
        result.res_state = "ok" if complete and not result.res_extra else "outdated"
    return result


def write_res(check: MapCheck, path: Path) -> Path | None:
    """Write ``maps/<map>.res`` for ``check`` next to map ``path`` (nothing
    when the map needs no extra files; an existing file is kept as .bak)."""
    lines = check.to_write
    target = Path(path).with_suffix(".res")
    if not lines:
        return None
    if target.is_file():
        target.with_name(target.name + ".bak").write_bytes(target.read_bytes())
    target.write_text(res_text(lines, check.needs.name), encoding="latin-1", newline="\n")
    return target


def read_mapcycle(path: Path) -> list[str]:
    """Map names of a ``mapcycle.txt`` (first word per line, ``//`` comments)."""
    if not Path(path).is_file():
        return []
    out = []
    for line in Path(path).read_text(encoding="latin-1").splitlines():
        words = line.split("//", 1)[0].split()
        if words:
            out.append(words[0].removesuffix(".bsp"))
    return out


__all__ = ["MapCheck", "MapNeeds", "SKY_SIDES", "check_map", "map_needs", "parse_res",
           "read_mapcycle", "res_text", "write_res"]
