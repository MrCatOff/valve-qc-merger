"""The project's sprite library: ``<project>/sprites/`` mirrors the game's ``sprites/``.

It holds effect sprites (muzzle flashes, glows, smoke: plugins
``precache_model`` them — model slots) and weapon HUDs (``weapon_*.txt`` and
the sheet sprites they draw from: only downloaded, ``precache_generic``).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from valve_qc_merger.sprite.hud import AMMO_ICON, WEAPON_ICON, pack, sheets_of, weapon_txt
from valve_qc_merger.sprite.spr import from_images, write_spr

if TYPE_CHECKING:
    from valve_qc_merger.project.model import Project

SUFFIXES = (".spr", ".txt")


def sprites_dir(project: Project) -> Path:
    return project.root / "sprites"


def sprite_path(project: Project, name: str) -> Path:
    """``name`` is the game path under sprites/ (``muzzle/flash1.spr``)."""
    return sprites_dir(project) / name


def list_sprites(project: Project) -> list[str]:
    base = sprites_dir(project)
    if not base.is_dir():
        return []
    return sorted((p.relative_to(base).as_posix() for p in base.rglob("*")
                   if p.is_file() and p.suffix.lower() in SUFFIXES), key=str.lower)


def import_sprites(project: Project, sources: list[Path]) -> list[str]:
    """Copy .spr/.txt files (or every one under folders); the path after a
    ``sprites`` folder is kept."""
    names = []
    for source in sources:
        source = Path(source)
        files = ([p for p in sorted(source.rglob("*")) if p.is_file()
                  and p.suffix.lower() in SUFFIXES] if source.is_dir() else [source])
        for path in files:
            if path.suffix.lower() not in SUFFIXES:
                continue
            parts = [p.lower() for p in path.parts]
            if "sprites" in parts:
                cut = len(parts) - 1 - parts[::-1].index("sprites")
                name = Path(*path.parts[cut + 1:]).as_posix()
            elif source.is_dir():
                name = path.relative_to(source.parent).as_posix()
            else:
                name = path.name
            target = sprite_path(project, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not (target.exists() and target.resolve() == path.resolve()):
                shutil.copy2(path, target)  # (re-importing a library file: nothing to copy)
            names.append(name)
    return names


def remove_sprite(project: Project, name: str) -> None:
    sprite_path(project, name).unlink(missing_ok=True)


def make_sprite(project: Project, name: str, images: list[np.ndarray], *,
                fmt: str = "additive", stype: str = "parallel") -> str:
    """Write ``sprites/<name>.spr`` from RGBA frames; returns its name."""
    if not name.lower().endswith(".spr"):
        name += ".spr"
    path = sprite_path(project, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(write_spr(from_images(images, fmt=fmt, stype=stype)))
    return name


def _fit(image: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Nearest-neighbour fit of an RGBA icon into ``size`` (aspect kept,
    centred on transparent)."""
    h, w = image.shape[:2]
    tw, th = size
    scale = min(tw / w, th / h)
    nw, nh = max(int(round(w * scale)), 1), max(int(round(h * scale)), 1)
    ys = (np.arange(nh) / scale).astype(int).clip(0, h - 1)
    xs = (np.arange(nw) / scale).astype(int).clip(0, w - 1)
    scaled = image[ys][:, xs]
    out = np.zeros((th, tw, 4), np.uint8)
    oy, ox = (th - nh) // 2, (tw - nw) // 2
    out[oy:oy + nh, ox:ox + nw] = scaled
    return out


def make_weapon_hud(project: Project, weapon: str, icon: np.ndarray, *,
                    selected: np.ndarray | None = None, ammo: np.ndarray | None = None,
                    sheet: str | None = None) -> list[str]:
    """``sprites/<weapon>.txt`` + its sheet ``sprites/<sheet>.spr`` from the
    weapon-list icon (fitted to 170×45), an optional brighter selected icon
    and an optional 24×24 ammo icon. Returns the names written."""
    weapon = weapon if weapon.startswith("weapon_") else f"weapon_{weapon}"
    sheet = sheet or f"640hud_{weapon.removeprefix('weapon_')}"
    icons = {"weapon": _fit(icon, WEAPON_ICON)}
    if selected is not None:
        icons["weapon_s"] = _fit(selected, WEAPON_ICON)
    if ammo is not None:
        icons["ammo"] = _fit(ammo, AMMO_ICON)
    (layout,) = pack({k: (v.shape[1], v.shape[0]) for k, v in icons.items()})
    canvas = np.zeros((layout.height, layout.width, 4), np.uint8)
    for key, (x, y, w, h) in layout.rects.items():
        canvas[y:y + h, x:x + w] = icons[key]
    sheet_name = make_sprite(project, sheet, [canvas], fmt="additive")
    ammo_entry = (sheet, *layout.rects["ammo"]) if "ammo" in layout.rects else None
    text = weapon_txt(weapon, sheet, layout.rects["weapon"], layout.rects.get("weapon_s"),
                      ammo_entry)
    txt_name = f"{weapon}.txt"
    sprite_path(project, txt_name).write_text(text, encoding="latin-1")
    return [txt_name, sheet_name]


def hud_files(project: Project) -> set[str]:
    """Library HUD files: every ``*.txt`` and the library sheets they use
    (game paths, ``sprites/...``)."""
    out: set[str] = set()
    names = {n.lower(): n for n in list_sprites(project)}
    for name in names.values():
        if name.lower().endswith(".txt"):
            out.add(f"sprites/{name}")
            text = sprite_path(project, name).read_text(encoding="latin-1", errors="replace")
            for sheet in sheets_of(text):
                local = sheet.removeprefix("sprites/").lower()
                if local in names:
                    out.add(f"sprites/{names[local]}")
    return out


def effect_sprites(project: Project) -> list[str]:
    """Library sprites that are not HUD sheets (``sprites/...``): plugins
    precache_model them."""
    hud = {h.lower() for h in hud_files(project)}
    return [f"sprites/{n}" for n in list_sprites(project)
            if n.lower().endswith(".spr") and f"sprites/{n}".lower() not in hud]


__all__ = ["effect_sprites", "hud_files", "import_sprites", "list_sprites", "make_sprite",
           "make_weapon_hud", "remove_sprite", "sprite_path", "sprites_dir"]
