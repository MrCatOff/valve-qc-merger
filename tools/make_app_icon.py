"""Render the studio's application icon: app.png, app.ico, app.icns.

    python tools/make_app_icon.py   # writes src/valve_qc_merger/studio/icons/app/

A rounded square in the theme's accent (a soft vertical gradient) with the
Lucide "boxes" glyph in white. The .ico (Windows exe) and .icns (macOS app)
are PNG-in-container files written by hand, so no platform tool is needed;
re-run after changing the look and commit the three files.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
OUT = ROOT / "src" / "valve_qc_merger" / "studio" / "icons" / "app"


def render(size: int) -> bytes:
    """The icon as PNG bytes, ``size`` px square."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF
    from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath
    from PySide6.QtSvg import QSvgRenderer

    from valve_qc_merger.studio import theme
    from valve_qc_merger.studio.icons import svg_text

    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    margin = size * 0.06  # macOS-style breathing room around the tile
    tile = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    path = QPainterPath()
    path.addRoundedRect(tile, tile.width() * 0.22, tile.width() * 0.22)
    gradient = QLinearGradient(tile.topLeft(), tile.bottomLeft())
    gradient.setColorAt(0.0, QColor(theme.TOKENS["accent_hover"]))
    gradient.setColorAt(1.0, QColor(theme.TOKENS["accent_press"]))
    painter.fillPath(path, gradient)
    glyph = svg_text("boxes").replace("currentColor", "#ffffff").replace(
        'stroke-width="2"', 'stroke-width="1.7"')
    renderer = QSvgRenderer(QByteArray(glyph.encode("utf-8")))
    inset = tile.width() * 0.2
    renderer.render(painter, tile.adjusted(inset, inset, -inset, -inset))
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def write_ico(path: Path, sizes: list[int]) -> None:
    """ICONDIR + entries, each a PNG (Windows Vista+ reads PNG entries)."""
    images = [render(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in zip(sizes, images, strict=True):
        side = 0 if size >= 256 else size  # 0 means 256
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    path.write_bytes(header + entries + blobs)


# icns element types holding PNG data, by pixel size (Retina variants share sizes)
ICNS_TYPES = [(b"ic11", 32), (b"ic12", 64), (b"ic07", 128), (b"ic13", 256),
              (b"ic08", 256), (b"ic14", 512), (b"ic09", 512), (b"ic10", 1024)]


def write_icns(path: Path) -> None:
    body = b""
    for kind, size in ICNS_TYPES:
        png = render(size)
        body += kind + struct.pack(">I", 8 + len(png)) + png
    path.write_bytes(b"icns" + struct.pack(">I", 8 + len(body)) + body)


def main() -> int:
    from PySide6.QtGui import QGuiApplication
    _app = QGuiApplication.instance() or QGuiApplication([])
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "app.png").write_bytes(render(512))
    write_ico(OUT / "app.ico", [16, 24, 32, 48, 64, 128, 256])
    write_icns(OUT / "app.icns")
    print(f"icon -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
