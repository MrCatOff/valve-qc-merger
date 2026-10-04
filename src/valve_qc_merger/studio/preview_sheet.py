"""The grid sheet of weapon previews: cards with the weapon's name."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath

from valve_qc_merger.preview.render import Preview
from valve_qc_merger.studio import theme

GAP = 12
LABEL = 28


def sheet(previews: list[Preview], names: list[str], columns: int = 4) -> QImage:
    """One image: a card per preview (rounded, the theme's surface colour),
    the weapon drawn on it and its name below. Safe off the GUI thread."""
    if not previews:
        return QImage()
    height, width = previews[0].rgba.shape[:2]
    columns = max(1, min(columns, len(previews)))
    rows = (len(previews) + columns - 1) // columns
    card_w, card_h = width + 2 * GAP, height + LABEL + GAP
    image = QImage(columns * card_w + GAP, rows * card_h + GAP + rows * GAP,
                   QImage.Format.Format_ARGB32)
    image.fill(QColor(theme.TOKENS["bg"]))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = QFont()
    font.setPixelSize(max(12, min(18, width // 20)))
    font.setBold(True)
    painter.setFont(font)
    for index, (preview, name) in enumerate(zip(previews, names, strict=True)):
        x = GAP + (index % columns) * card_w
        y = GAP + (index // columns) * (card_h + GAP)
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, card_w - GAP, card_h), 8, 8)
        painter.fillPath(path, QColor(theme.TOKENS["surface"]))
        rgba = preview.rgba
        picture = QImage(rgba.tobytes(), rgba.shape[1], rgba.shape[0], rgba.shape[1] * 4,
                         QImage.Format.Format_RGBA8888)
        painter.drawImage(int(x + GAP / 2), int(y + GAP / 2), picture)
        painter.setPen(QColor(theme.TOKENS["text"]))
        painter.drawText(QRectF(x + GAP / 2, y + height + GAP / 2, width, LABEL),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
    painter.end()
    return image


__all__ = ["sheet"]
