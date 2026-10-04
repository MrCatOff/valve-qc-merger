"""A folder of weapon previews for a server: one PNG per weapon + a catalog.

``images/<weapon>.png`` (transparent, named without the ``v_`` prefix so a
plugin can build the path from the weapon name), ``index.html`` (a grid of
cards — name, category, the asset's notes as the description — that works
as a MOTD page or on a site) and ``weapons.txt`` (one ``name|category|image
|description`` line per weapon, for plugins that print their own menus).
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from valve_qc_merger.preview.render import Options, Preview, render_model
from valve_qc_merger.services.base import Reporter

if TYPE_CHECKING:
    from valve_qc_merger.project.model import Asset, Project


@dataclass
class Entry:
    name: str  # without v_
    asset: str
    category: str
    notes: str
    image: str  # relative to the catalog folder
    hands: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class Catalog:
    folder: Path
    entries: list[Entry] = field(default_factory=list)
    previews: list[Preview] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # "<asset>: why"


def weapon_name(asset: str) -> str:
    return asset[2:] if asset.lower().startswith("v_") else asset


def preview_assets(project: Project, category: str | None = None) -> list[Asset]:
    """The view models to preview: imported ones (a retargeted copy shows
    the same weapon), optionally of one category."""
    return sorted((a for a in project.assets.values()
                   if a.kind == "v" and not a.derived
                   and (category is None or a.category == category)),
                  key=lambda a: (a.category.lower(), a.name.lower()))


def build_catalog(project: Project, assets: list[Asset], folder: Path,
                  options: Options | None = None, *, title: str = "",
                  reporter: Reporter | None = None) -> Catalog:
    """Render ``assets`` into ``folder`` and write the catalog files."""
    reporter = reporter or Reporter()
    folder = Path(folder)
    (folder / "images").mkdir(parents=True, exist_ok=True)
    catalog = Catalog(folder)
    for done, asset in enumerate(assets):
        reporter.check()
        reporter.progress(done, len(assets), asset.name)
        try:
            preview = render_model(project.asset_dir(asset.name), options)
        except Exception as exc:  # noqa: BLE001 - one broken model must not stop the rest
            catalog.failed.append(f"{asset.name}: {exc}")
            continue
        name = weapon_name(asset.name)
        image = f"images/{name}.png"
        (folder / image).write_bytes(preview.png())
        catalog.previews.append(preview)
        catalog.entries.append(Entry(name, asset.name, asset.category, asset.notes.strip(),
                                     image, preview.hands, preview.warnings))
    (folder / "index.html").write_text(catalog_html(catalog.entries,
                                                    title or f"{project.name} — weapons"),
                                       encoding="utf-8")
    (folder / "weapons.txt").write_text(weapons_txt(catalog.entries), encoding="utf-8")
    return catalog


def weapons_txt(entries: list[Entry]) -> str:
    lines = ["# name|category|image|description"]
    for e in entries:
        text = " ".join(e.notes.split()).replace("|", "/")
        lines.append(f"{e.name}|{e.category}|{e.image}|{text}")
    return "\n".join(lines) + "\n"


_STYLE = """
body{margin:0;padding:16px;background:#121419;color:#e4e7ee;
font:14px/1.4 Segoe UI,Tahoma,Arial,sans-serif}
h1{font-size:20px;margin:0 0 12px}h2{font-size:15px;color:#a0a7b6;margin:20px 0 8px;
text-transform:uppercase;letter-spacing:.06em}
.grid{display:flex;flex-wrap:wrap;gap:10px}
.card{width:260px;background:#1f232b;border:1px solid #2a2f39;border-radius:8px;
padding:8px;box-sizing:border-box}
.card img{width:100%;display:block;background:radial-gradient(#30363f,#1f232b 70%);
border-radius:6px}
.name{font-weight:600;margin-top:6px}.notes{color:#a0a7b6;font-size:12px;margin-top:2px;
white-space:pre-line}
"""


def catalog_html(entries: list[Entry], title: str) -> str:
    """A self-contained page (no scripts): a section of cards per category."""
    esc = html.escape
    out = ["<!doctype html>", "<html><head><meta charset='utf-8'>",
           "<meta name='viewport' content='width=device-width, initial-scale=1'>",
           f"<title>{esc(title)}</title><style>{_STYLE}</style></head><body>",
           f"<h1>{esc(title)}</h1>"]
    categories: dict[str, list[Entry]] = {}
    for entry in entries:
        categories.setdefault(entry.category or "Other", []).append(entry)
    for category, members in categories.items():
        if len(categories) > 1:
            out.append(f"<h2>{esc(category)}</h2>")
        out.append("<div class='grid'>")
        for e in members:
            notes = f"<div class='notes'>{esc(e.notes)}</div>" if e.notes else ""
            out.append(f"<div class='card' id='{esc(e.name)}'><img src='{esc(e.image)}' "
                       f"alt='{esc(e.name)}'><div class='name'>{esc(e.name)}</div>{notes}"
                       "</div>")
        out.append("</div>")
    out.append("</body></html>")
    return "\n".join(out) + "\n"


__all__ = ["Catalog", "Entry", "build_catalog", "catalog_html", "preview_assets",
           "weapon_name", "weapons_txt"]
