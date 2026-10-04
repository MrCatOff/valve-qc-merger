"""Lucide icons (ISC, ``icons/LICENSE``) tinted to the studio theme.

The SVGs draw with ``stroke="currentColor"``: the colour is substituted
before rendering, so one file serves every state — :func:`icon` builds a
QIcon whose disabled mode is the theme's faint text colour and whose
normal mode is the requested colour (default: the regular text colour).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from valve_qc_merger.studio import theme

ICON_DIR = Path(__file__).resolve().parent / "icons"
_SVG: dict[str, str] = {}
_ICONS: dict[tuple[str, str], QIcon] = {}


def svg_text(name: str) -> str:
    if name not in _SVG:
        _SVG[name] = (ICON_DIR / f"{name}.svg").read_text(encoding="utf-8")
    return _SVG[name]


def pixmap(name: str, color: str, size: int, *, scale: float = 1.0,
           stroke: float | None = None) -> QPixmap:
    """``name`` rendered ``size`` px square in ``color`` (``scale``: device
    pixel ratio, for crisp HiDPI pixmaps)."""
    text = svg_text(name).replace("currentColor", color)
    if stroke is not None:
        text = text.replace('stroke-width="2"', f'stroke-width="{stroke:g}"')
    renderer = QSvgRenderer(QByteArray(text.encode("utf-8")))
    side = max(1, round(size * scale))
    out = QPixmap(side, side)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter, QRectF(0, 0, side, side))
    painter.end()
    out.setDevicePixelRatio(scale)
    return out


def icon(name: str, color: str | None = None) -> QIcon:
    """A theme-tinted icon: ``color`` (default text) normally, faint when
    disabled; rendered at 1x and 2x for HiDPI screens."""
    color = color or theme.TOKENS["text"]
    key = (name, color)
    if key not in _ICONS:
        out = QIcon()
        for size in (16, 20, 24):
            for scale in (1.0, 2.0):
                out.addPixmap(pixmap(name, color, size, scale=scale), QIcon.Mode.Normal)
                out.addPixmap(pixmap(name, theme.TOKENS["faint"], size, scale=scale),
                              QIcon.Mode.Disabled)
        _ICONS[key] = out
    return _ICONS[key]


ICON_SIZE = QSize(18, 18)

__all__ = ["ICON_DIR", "ICON_SIZE", "icon", "pixmap", "svg_text"]
