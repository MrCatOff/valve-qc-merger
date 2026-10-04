"""The job log with levels, and the toast that reports a finished job.

Every line is classified once (error, warning, success, a job header, or
plain info) and shown in that colour; the bar on top filters by level or
text, copies what is shown and clears. Asset names in a line are links:
clicking one selects the asset. Counts of warnings and errors drive the
status-bar button, which shows and hides the (by default hidden) log.
"""

from __future__ import annotations

import html
import re

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.studio import theme
from valve_qc_merger.studio.icons import icon

LEVELS = ("error", "warning", "success", "header", "info")
_ERROR = re.compile(r"\b(?:error|errors|failed|fail|FAIL|FAILED|Traceback|Exception)\b|✗")
_WARNING = re.compile(r"\b(?:warn|warning|warnings|WARN|WARNING)\b|⚠")
_SUCCESS = re.compile(r"\b(?:PASS|OK|done)\b|✓")
MAX_ENTRIES = 20_000


def classify(line: str) -> str:
    """The level of one log message (first match wins: error > warning >
    header > success > info). "0 failed" in a summary is not an error."""
    text = re.sub(r"\b0 (?:failed|errors?|warnings?)\b", "", line)
    if _ERROR.search(text):
        return "error"
    if _WARNING.search(text):
        return "warning"
    if line.startswith("── "):
        return "header"
    if _SUCCESS.search(text):
        return "success"
    return "info"


LEVEL_TOKENS = {"error": "danger", "warning": "warning", "success": "success",
                "header": "text", "info": "muted"}
# which levels each filter shows
FILTERS = {"all": set(LEVELS), "warning": {"warning", "error"}, "error": {"error"}}


class LogPanel(QWidget):
    """Bounded, levelled job log (``append_line`` / ``toPlainText`` as before)."""

    asset_clicked = Signal(str)
    counts_changed = Signal(int, int)  # warnings, errors

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[tuple[str, str]] = []
        self._assets: frozenset[str] = frozenset()
        self._asset_re: re.Pattern[str] | None = None
        self.warnings = self.errors = 0
        self._filter = "all"

        bar = QHBoxLayout()
        bar.setContentsMargins(8, 4, 8, 4)
        bar.setSpacing(4)
        self.filter_group = QButtonGroup(self)
        self.filter_buttons: dict[str, QToolButton] = {}
        for key, text in (("all", "All"), ("warning", "Warnings"), ("error", "Errors")):
            button = QToolButton()
            button.setText(text)
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.toggled.connect(lambda on, k=key: on and self._set_filter(k))
            self.filter_group.addButton(button)
            self.filter_buttons[key] = button
            bar.addWidget(button)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter the log")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(260)
        self.search.textChanged.connect(lambda _t: self._render())
        bar.addSpacing(8)
        bar.addWidget(self.search)
        bar.addStretch(1)
        copy = QToolButton()
        copy.setIcon(icon("copy"))
        copy.setToolTip("Copy the shown lines")
        copy.setAutoRaise(True)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.shown_text()))
        clear = QToolButton()
        clear.setIcon(icon("eraser"))
        clear.setToolTip("Clear the log")
        clear.setAutoRaise(True)
        clear.clicked.connect(self.clear)
        bar.addWidget(copy)
        bar.addWidget(clear)

        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.view.setLineWrapMode(QTextBrowser.LineWrapMode.NoWrap)
        self.view.anchorClicked.connect(self._link)
        self.view.document().setDefaultStyleSheet(
            f"a {{ color: {theme.TOKENS['accent']}; text-decoration: none; }}")
        self.view.document().setMaximumBlockCount(MAX_ENTRIES * 2)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        holder = QWidget()
        theme.set_role(holder, "dock-bar")
        holder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        holder.setLayout(bar)
        layout.addWidget(holder)
        layout.addWidget(self.view, 1)
        self.filter_buttons["all"].setChecked(True)  # needs the view

    # -- content -------------------------------------------------------------
    def append_line(self, line: str) -> None:
        level = classify(line)
        self._entries.append((level, line))
        if level == "warning":
            self.warnings += 1
        elif level == "error":
            self.errors += 1
        if level in ("warning", "error"):
            self.counts_changed.emit(self.warnings, self.errors)
        if len(self._entries) > MAX_ENTRIES:
            del self._entries[: MAX_ENTRIES // 10]
            self._render()
            return
        if self._shown(level, line):
            bar = self.view.verticalScrollBar()
            at_end = bar.value() >= bar.maximum() - 4
            self.view.append(self._html(level, line))
            if at_end:
                bar.setValue(bar.maximum())

    def clear(self) -> None:
        self._entries.clear()
        self.warnings = self.errors = 0
        self.view.clear()
        self.counts_changed.emit(0, 0)

    def toPlainText(self) -> str:  # noqa: N802 - mirrors QPlainTextEdit
        return "\n".join(line for _level, line in self._entries)

    def shown_text(self) -> str:
        return "\n".join(line for level, line in self._entries if self._shown(level, line))

    def set_assets(self, names: set[str] | frozenset[str]) -> None:
        """Asset names to turn into links (longest first, whole words)."""
        names = frozenset(names)
        if names == self._assets:
            return
        self._assets = names
        self._asset_re = (re.compile(r"(?<![\w.])(" + "|".join(
            re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?![\w])")
            if names else None)
        self._render()

    # -- filtering / rendering ----------------------------------------------
    def _set_filter(self, key: str) -> None:
        self._filter = key
        self._render()

    def _shown(self, level: str, line: str) -> bool:
        needle = self.search.text().strip().lower()
        return level in FILTERS[self._filter] and (not needle or needle in line.lower())

    def _html(self, level: str, line: str) -> str:
        text = html.escape(line)
        if self._asset_re is not None:
            text = self._asset_re.sub(lambda m: f'<a href="asset:{m.group(1)}">'
                                      f"{m.group(1)}</a>", text)
        colour = theme.TOKENS[LEVEL_TOKENS[level]]
        weight = "font-weight:600;" if level in ("header", "error") else ""
        text = text.replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
        return f'<span style="color:{colour};{weight}">{text}</span>'

    def _render(self) -> None:
        self.view.clear()
        shown = [self._html(level, line) for level, line in self._entries
                 if self._shown(level, line)]
        if shown:
            self.view.setHtml("<br>".join(shown))
            bar = self.view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _link(self, url: QUrl) -> None:
        text = url.toString()
        if text.startswith("asset:"):
            self.asset_clicked.emit(text[len("asset:"):])


class Toast(QFrame):
    """A small card in the window's bottom-right corner: what just finished.
    Success fades after a few seconds; a failure stays until closed."""

    log_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        theme.set_role(self, "toast")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 8, 10)
        layout.setSpacing(10)
        self.icon = QLabel()
        layout.addWidget(self.icon, 0)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.title = QLabel()
        self.title.setStyleSheet("font-weight: 600;")
        self.detail = QLabel()
        theme.set_role(self.detail, "hint")
        self.detail.setWordWrap(True)
        self.detail.setMaximumWidth(360)
        texts.addWidget(self.title)
        texts.addWidget(self.detail)
        layout.addLayout(texts, 1)
        self.log_button = QPushButton("Show log")
        theme.set_role(self.log_button, "link")
        self.log_button.clicked.connect(self.log_requested)
        self.log_button.clicked.connect(self.hide)
        layout.addWidget(self.log_button)
        close = QToolButton()
        close.setIcon(icon("x", theme.TOKENS["muted"]))
        close.setAutoRaise(True)
        close.clicked.connect(self.hide)
        layout.addWidget(close)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, level: str, title: str, detail: str = "") -> None:
        from valve_qc_merger.studio.icons import pixmap
        name, token = {"success": ("circle-check", "success"),
                       "warning": ("triangle-alert", "warning"),
                       "error": ("circle-x", "danger")}[level]
        self.icon.setPixmap(pixmap(name, theme.TOKENS[token], 20,
                                   scale=self.devicePixelRatioF() or 1.0))
        self.title.setText(title)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.log_button.setVisible(level != "success")
        self.setProperty("level", level)
        self.style().unpolish(self)
        self.style().polish(self)
        self.adjustSize()
        self.place()
        self.show()
        self.raise_()
        self.timer.stop()
        if level != "error":
            self.timer.start(6000)

    def place(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        status = getattr(parent, "statusBar", None)
        bottom = status().height() if callable(status) else 0
        self.move(parent.width() - self.width() - 16,
                  parent.height() - self.height() - bottom - 16)


__all__ = ["LEVELS", "LogPanel", "Toast", "classify"]
