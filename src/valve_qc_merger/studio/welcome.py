"""The welcome screen: shown in place of the viewport while no project is open."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.studio import theme
from valve_qc_merger.studio.icons import icon, pixmap

# (icon, title, what it does, shortcut)
STEPS = [
    ("file-down", "Import models",
     "Add .mdl files or decompiled folders. Models are sorted by kind (v_, p_, w_, "
     "players, zombie hands) and can be grouped into categories.", "Ctrl+I"),
    ("hand", "Put them on our hands",
     "Retarget swaps a weapon's hands for ours and moves every animation onto them; "
     "nudge the grip and compare before/after in the viewport.", "Ctrl+R"),
    ("package-plus", "Build, compile, deploy",
     "A build merges a category into one model under the bodygroup limits, "
     "compiles it with studiomdl and copies it into the game folder.", "Ctrl+B"),
]


def _card() -> QFrame:
    card = QFrame()
    theme.set_role(card, "card")
    return card


def _label(text: str, role: str | None = None, *, wrap: bool = False) -> QLabel:
    label = QLabel(text)
    if role:
        theme.set_role(label, role)
    label.setWordWrap(wrap)
    return label


class WelcomePage(QScrollArea):
    """Title, New/Open, recent projects and a three-step getting started."""

    new_requested = Signal()
    open_requested = Signal()
    recent_requested = Signal(str)

    def __init__(self, version: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        # never raise the central widget's minimum width (docks keep their size)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        page = QWidget()
        theme.set_role(page, "page")
        self.setWidget(page)
        outer = QHBoxLayout(page)
        outer.setContentsMargins(32, 48, 32, 32)
        column = QWidget()
        column.setMaximumWidth(860)
        outer.addStretch(1)
        outer.addWidget(column, 100)
        outer.addStretch(1)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(16)
        logo = QLabel()
        logo.setPixmap(pixmap("boxes", theme.TOKENS["accent"], 44,
                              scale=self.devicePixelRatioF() or 1.0, stroke=1.6))
        logo.setAlignment(Qt.AlignmentFlag.AlignTop)
        head.addWidget(logo)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        title_row = QHBoxLayout()
        title_row.addWidget(_label("valve-qc-merger Studio", "title"))
        if version:
            title_row.addWidget(_label(f"v{version}", "kbd"), 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addStretch(1)
        titles.addLayout(title_row)
        titles.addWidget(_label("Merge CS 1.6 and CSO GoldSource models: put weapons on one "
                                "pair of hands, pack them into bodygroups, compile and "
                                "deploy.", "subtitle", wrap=True))
        head.addLayout(titles, 1)
        layout.addLayout(head)
        layout.addSpacing(20)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.new_button = QPushButton(icon("file-plus", theme.TOKENS["on_accent"]),
                                      "  New project")
        theme.set_primary(self.new_button)
        self.new_button.clicked.connect(self.new_requested)
        self.open_button = QPushButton(icon("folder-open"), "  Open project…")
        self.open_button.clicked.connect(self.open_requested)
        for button in (self.new_button, self.open_button):
            button.setMinimumHeight(34)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addSpacing(24)

        cards = QHBoxLayout()
        cards.setSpacing(16)
        recent = _card()
        recent_layout = QVBoxLayout(recent)
        recent_layout.setContentsMargins(18, 16, 18, 16)
        recent_layout.setSpacing(6)
        recent_layout.addWidget(_label("RECENT PROJECTS", "section"))
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(2)
        recent_layout.addLayout(self.recent_box)
        recent_layout.addStretch(1)
        cards.addWidget(recent, 1)

        start = _card()
        start_layout = QVBoxLayout(start)
        start_layout.setContentsMargins(18, 16, 18, 16)
        start_layout.setSpacing(12)
        start_layout.addWidget(_label("GETTING STARTED", "section"))
        for number, (name, title, text, keys) in enumerate(STEPS, 1):
            row = QGridLayout()
            row.setHorizontalSpacing(12)
            row.setVerticalSpacing(2)
            badge = QLabel()
            badge.setPixmap(pixmap(name, theme.TOKENS["accent"], 20,
                                   scale=self.devicePixelRatioF() or 1.0))
            badge.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
            row.addWidget(badge, 0, 0, 2, 1)
            heading = QHBoxLayout()
            heading.addWidget(_label(f"{number}. {title}"))
            heading.addStretch(1)
            heading.addWidget(_label(keys, "kbd"))
            row.addLayout(heading, 0, 1)
            row.addWidget(_label(text, "hint", wrap=True), 1, 1)
            row.setColumnStretch(1, 1)
            start_layout.addLayout(row)
        start_layout.addStretch(1)
        cards.addWidget(start, 1)
        layout.addLayout(cards)
        layout.addStretch(1)
        self.set_recent([])

    def set_recent(self, paths: list[str]) -> None:
        while self.recent_box.count():
            widget = self.recent_box.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        if not paths:
            self.recent_box.addWidget(_label("No recent projects yet — create one or open "
                                             "a project folder.", "hint", wrap=True))
            return
        for path in paths:
            entry = QPushButton(Path(path).name)
            theme.set_role(entry, "link")
            entry.setToolTip(path)
            entry.setCursor(Qt.CursorShape.PointingHandCursor)
            entry.clicked.connect(lambda _=False, p=path: self.recent_requested.emit(p))
            holder = QWidget()
            line = QVBoxLayout(holder)
            line.setContentsMargins(0, 2, 0, 4)
            line.setSpacing(0)
            line.addWidget(entry, 0, Qt.AlignmentFlag.AlignLeft)
            where = _label(str(Path(path).parent), "faint")
            where.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
            where.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            line.addWidget(where)
            self.recent_box.addWidget(holder)


__all__ = ["STEPS", "WelcomePage"]
