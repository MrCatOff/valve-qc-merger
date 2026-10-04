"""Shared pieces of the studio's dialogs: header, sections, option cards.

Every dialog opens the same way — a heading and one line on what it does —
groups its fields under small-caps sections instead of framed group boxes,
and uses the same margins, so they read as one application.
"""

from __future__ import annotations

from PySide6.QtCore import QLocale, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.studio import theme
from valve_qc_merger.studio.icons import icon, pixmap

MARGINS = (20, 18, 20, 16)

# numbers are typed and shown with a decimal point whatever the system locale
NUMBER_LOCALE = QLocale(QLocale.Language.C)
NUMBER_LOCALE.setNumberOptions(QLocale.NumberOption.OmitGroupSeparator)


def number_spin(spin: QAbstractSpinBox) -> QAbstractSpinBox:
    spin.setLocale(NUMBER_LOCALE)
    return spin


def header(title: str, subtitle: str = "") -> QWidget:
    """A dialog's heading and one line on what it does."""
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 4)
    layout.setSpacing(4)
    heading = QLabel(title)
    theme.set_role(heading, "heading")
    layout.addWidget(heading)
    if subtitle:
        line = QLabel(subtitle)
        theme.set_role(line, "hint")
        line.setWordWrap(True)
        layout.addWidget(line)
    return holder


def section(title: str) -> QLabel:
    label = QLabel(title.upper())
    theme.set_role(label, "section")
    label.setContentsMargins(0, 8, 0, 0)
    return label


def hint(text: str) -> QLabel:
    label = QLabel(text)
    theme.set_role(label, "hint")
    label.setWordWrap(True)
    return label


def form() -> QFormLayout:
    out = QFormLayout()
    out.setContentsMargins(0, 0, 0, 0)
    out.setHorizontalSpacing(14)
    out.setVerticalSpacing(8)
    out.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    out.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return out


def dialog_layout(dialog: QWidget) -> QVBoxLayout:
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(*MARGINS)
    layout.setSpacing(8)
    # a dialog never shrinks below its content (wrapped hints included)
    layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
    return layout


class Collapsible(QWidget):
    """A section whose body folds away behind its title (chevron)."""

    toggled = Signal(bool)

    def __init__(self, title: str, body: QWidget | QLayout, *, open_: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.button = QToolButton()
        self.button.setText(title.upper())
        self.button.setCheckable(True)
        self.button.setAutoRaise(True)
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        theme.set_role(self.button, "section")
        layout.addWidget(self.button, 0, Qt.AlignmentFlag.AlignLeft)
        if isinstance(body, QLayout):
            holder = QWidget()
            holder.setLayout(body)
            body = holder
        self.body = body
        layout.addWidget(body)
        self.button.toggled.connect(self._show)
        self.button.setChecked(open_)
        self._show(open_)

    def _show(self, shown: bool) -> None:
        self.button.setIcon(icon("chevron-down" if shown else "chevron-right",
                                 theme.TOKENS["muted"]))
        self.body.setVisible(shown)
        self.toggled.emit(shown)

    def is_open(self) -> bool:
        return self.button.isChecked()


class OptionCard(QPushButton):
    """A checkable card: icon, title, one or two lines of description (a
    choice between modes, picked like a radio button in a QButtonGroup)."""

    def __init__(self, icon_name: str, title: str, text: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        # never the dialog's default button (Enter = Run, and no accent fill)
        self.setAutoDefault(False)
        self.setDefault(False)
        theme.set_role(self, "card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(text)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        self.icon_label = QLabel()
        self.icon_label.setPixmap(pixmap(icon_name, theme.TOKENS["accent"], 22, scale=2.0))
        self.icon_label.setFixedSize(QSize(24, 24))
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-weight: 600;")
        self.text_label = QLabel(text)
        theme.set_role(self.text_label, "hint")
        self.text_label.setWordWrap(True)
        texts.addWidget(self.title_label)
        texts.addWidget(self.text_label)
        layout.addLayout(texts, 1)
        for label in (self.icon_label, self.title_label, self.text_label):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def sizeHint(self) -> QSize:  # noqa: N802 - the layout decides, not the text
        return self.layout().sizeHint()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.layout().minimumSize()

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self.layout().totalHeightForWidth(width)


__all__ = ["Collapsible", "NUMBER_LOCALE", "OptionCard", "dialog_layout", "form", "header",
           "hint", "number_spin", "section"]
