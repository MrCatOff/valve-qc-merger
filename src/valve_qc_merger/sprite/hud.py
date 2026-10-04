"""Weapon HUD files: ``sprites/weapon_<name>.txt`` and the icon sheets.

The client reads a weapon's HUD from ``sprites/<weapon>.txt``: a count, then
one line per entry — ``name resolution sheet x y width height``. Icons
live in sheet sprites (``sprites/<sheet>.spr``, additive like Valve's
``640hud*``: black is transparent). :func:`pack` lays icons out on as few
256×256 sheets as possible; :func:`weapon_txt` writes the file, the 640
entries pointing at the custom sheet and the 320 entries at the same
pixels (the low-resolution HUD scales them).
"""

from __future__ import annotations

from dataclasses import dataclass, field

SHEET = 256  # HUD sheets stay within 256x256
WEAPON_ICON = (170, 45)  # 640 HUD weapon-list icon
AMMO_ICON = (24, 24)
# stock crosshair art every CS client has (used unless the plugin draws its own)
STOCK_CROSSHAIR = ("crosshairs", 0, 48, 24, 24)
STOCK_AUTOAIM = ("crosshairs", 0, 72, 24, 24)


@dataclass
class Sheet:
    index: int
    width: int = SHEET
    height: int = 0
    rects: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)


def pack(sizes: dict[str, tuple[int, int]], sheet: int = SHEET) -> list[Sheet]:
    """Shelf-pack named (w, h) icons into ``sheet``-square sheets."""
    sheets: list[Sheet] = []
    order = sorted(sizes, key=lambda n: (-sizes[n][1], -sizes[n][0], n))
    current = None
    x = y = shelf = 0
    for name in order:
        w, h = sizes[name]
        if w > sheet or h > sheet:
            raise ValueError(f"icon {name} ({w}x{h}) is larger than a {sheet} sheet")
        if current is None or x + w > sheet:
            x, y, shelf = 0, y + shelf, 0
        if current is None or y + h > sheet:
            current = Sheet(len(sheets))
            sheets.append(current)
            x = y = shelf = 0
        current.rects[name] = (x, y, w, h)
        current.height = max(current.height, y + h)
        x += w
        shelf = max(shelf, h)
    for s in sheets:  # sprite sizes: multiples of 8 keep every renderer happy
        s.height = min(sheet, -(-s.height // 8) * 8)
    return sheets


def weapon_txt(weapon: str, sheet: str, weapon_rect: tuple[int, int, int, int],
               selected_rect: tuple[int, int, int, int] | None = None,
               ammo: tuple[str, int, int, int, int] | None = None) -> str:
    """The ``sprites/<weapon>.txt`` of one weapon (``sheet`` without .spr)."""
    selected_rect = selected_rect or weapon_rect
    lines = []
    for resolution in (320, 640):
        lines.append(("weapon", resolution, sheet, *weapon_rect))
        lines.append(("weapon_s", resolution, sheet, *selected_rect))
        if ammo is not None:
            lines.append(("ammo", resolution, *ammo))
    lines.append(("crosshair", 640, *STOCK_CROSSHAIR))
    lines.append(("autoaim", 640, *STOCK_AUTOAIM))
    body = "\n".join(f"{name}\t{res} {sheet_name}\t{x}\t{y}\t{w}\t{h}"
                     for name, res, sheet_name, x, y, w, h in lines)
    return f"{len(lines)}\n{body}\n"


def parse_txt(text: str) -> list[tuple[str, int, str, int, int, int, int]]:
    """Entries of a HUD txt (malformed lines skipped)."""
    out = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) == 7:
            try:
                out.append((parts[0], int(parts[1]), parts[2], *map(int, parts[3:])))
            except ValueError:
                continue
    return out


def sheets_of(text: str) -> set[str]:
    """Sheet sprites a HUD txt draws from (``sprites/<sheet>.spr``)."""
    return {f"sprites/{entry[2]}.spr" for entry in parse_txt(text)}


__all__ = ["AMMO_ICON", "SHEET", "STOCK_AUTOAIM", "STOCK_CROSSHAIR", "Sheet", "WEAPON_ICON",
           "pack", "parse_txt", "sheets_of", "weapon_txt"]
