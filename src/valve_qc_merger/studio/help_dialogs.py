"""Help ▸ Keyboard shortcuts and Help ▸ About."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QMenuBar,
    QScrollArea,
    QWidget,
)

from valve_qc_merger import __version__
from valve_qc_merger.studio import dialog_kit as kit
from valve_qc_merger.studio import theme
from valve_qc_merger.studio.icons import ICON_DIR

REPOSITORY = "https://github.com/MrCatOff/valve-qc-merger"
DOCS_URL = f"{REPOSITORY}/tree/main/docs"
APP_ICON = ICON_DIR / "app" / "app.png"

# keys that are not menu actions (they work with the viewport focused)
VIEWPORT_KEYS = [
    ("Space", "Play / pause"), ("← / →", "Previous / next frame"),
    ("F", "Frame the model (also double-click)"), ("B", "Before / after (derived assets)"),
    ("Drag · right-drag · wheel", "Orbit · pan · zoom"),
]


def menu_shortcuts(bar: QMenuBar) -> list[tuple[str, list[tuple[str, str]]]]:
    """``[(menu title, [(keys, action text)])]`` of every action with a shortcut."""
    out = []
    for menu_action in bar.actions():
        menu = menu_action.menu()
        if menu is None:
            continue
        rows = []
        for action in menu.actions():
            if isinstance(action, QAction) and not action.shortcut().isEmpty():
                keys = action.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
                rows.append((keys, action.text().replace("&", "").rstrip("…")))
        if rows:
            out.append((menu_action.text().replace("&", ""), rows))
    return out


def _kbd(text: str) -> QLabel:
    label = QLabel(text)
    theme.set_role(label, "kbd")
    return label


class ShortcutsDialog(QDialog):
    def __init__(self, bar: QMenuBar, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        layout = kit.dialog_layout(self)
        layout.addWidget(kit.header("Keyboard shortcuts", "Menu shortcuts work anywhere in "
                                    "the window; viewport keys when it has the focus."))
        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)
        row = 0
        self.rows = 0
        for title, entries in [*menu_shortcuts(bar), ("Viewport", VIEWPORT_KEYS)]:
            grid.addWidget(kit.section(title), row, 0, 1, 2)
            row += 1
            for keys, text in entries:
                grid.addWidget(_kbd(keys), row, 0, Qt.AlignmentFlag.AlignLeft)
                grid.addWidget(QLabel(text), row, 1)
                row += 1
                self.rows += 1
        grid.setColumnStretch(1, 1)
        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(480, 560)


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About valve-qc-merger Studio")
        layout = kit.dialog_layout(self)
        logo = QLabel()
        ratio = self.devicePixelRatioF() or 1.0
        art = QPixmap(str(APP_ICON)).scaled(
            int(72 * ratio), int(72 * ratio), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        art.setDevicePixelRatio(ratio)
        logo.setPixmap(art)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo)
        title = QLabel("valve-qc-merger Studio")
        theme.set_role(title, "heading")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        version = QLabel(f"Version {__version__}")
        theme.set_role(version, "hint")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(version)
        text = QLabel(
            "Merge CS 1.6 and CSO GoldSource models: put weapons on one pair of hands, "
            "pack them into bodygroups, compile and deploy.<br><br>"
            f'<a href="{REPOSITORY}">{REPOSITORY.removeprefix("https://")}</a>'
            "<br><br>MIT license · icons by Lucide (ISC) · built with Qt / PySide6 (LGPL)")
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text.setOpenExternalLinks(True)
        text.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(420, self.sizeHint().height())


__all__ = ["APP_ICON", "AboutDialog", "DOCS_URL", "ShortcutsDialog", "menu_shortcuts"]
