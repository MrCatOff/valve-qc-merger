"""The studio's dark theme: design tokens -> QPalette + one style sheet.

Every colour in the UI comes from :data:`TOKENS`; widgets never hard-code
colours — they mark themselves with a ``role`` property (``hint``,
``warning``, ``title``, ``card``, ...) that the style sheet styles, or ask
:func:`color`. Arrows and check marks the style sheet needs are Lucide icons
rendered once per run into a cache folder (QSS takes image files only).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QWidget

TOKENS: dict[str, str] = {
    # surfaces, darkest first
    "bg": "#121419",  # window frame, dock title bars, status bar
    "panel": "#181b21",  # dock contents
    "surface": "#1f232b",  # inputs, cards, lists in forms
    "raised": "#272c36",  # buttons
    "hover": "#30363f",
    "border": "#2a2f39",
    "border_strong": "#3a414e",
    # text
    "text": "#e4e7ee",
    "muted": "#a0a7b6",
    "faint": "#636a79",
    # accent + states
    "accent": "#5b8cff",
    "accent_hover": "#7ba3ff",
    "accent_press": "#4a77e0",
    "accent_soft": "#25345a",  # selection / checked background
    "on_accent": "#ffffff",
    "success": "#3fb68b",
    "warning": "#e5a54b",
    "danger": "#e5534b",
}

RADIUS = 6
FONT_PX = 13


def color(name: str) -> QColor:
    return QColor(TOKENS[name])


def _palette() -> QPalette:
    t = TOKENS
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: t["panel"],
        QPalette.ColorRole.WindowText: t["text"],
        QPalette.ColorRole.Base: t["surface"],
        QPalette.ColorRole.AlternateBase: t["raised"],
        QPalette.ColorRole.Text: t["text"],
        QPalette.ColorRole.Button: t["raised"],
        QPalette.ColorRole.ButtonText: t["text"],
        QPalette.ColorRole.BrightText: t["on_accent"],
        QPalette.ColorRole.Highlight: t["accent"],
        QPalette.ColorRole.HighlightedText: t["on_accent"],
        QPalette.ColorRole.ToolTipBase: t["raised"],
        QPalette.ColorRole.ToolTipText: t["text"],
        QPalette.ColorRole.PlaceholderText: t["faint"],
        QPalette.ColorRole.Link: t["accent"],
        QPalette.ColorRole.Light: t["hover"],
        QPalette.ColorRole.Midlight: t["raised"],
        QPalette.ColorRole.Mid: t["border_strong"],
        QPalette.ColorRole.Dark: t["bg"],
        QPalette.ColorRole.Shadow: "#000000",
    }
    for role, value in roles.items():
        p.setColor(role, QColor(value))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText,
                 QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(t["faint"]))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight,
               QColor(t["border_strong"]))
    return p


def _assets() -> dict[str, str]:
    """QSS image files (1x + @2x), rendered from Lucide in theme colours."""
    from valve_qc_merger.studio.icons import pixmap
    folder = Path(tempfile.gettempdir()) / "valve-qc-studio-theme"
    folder.mkdir(parents=True, exist_ok=True)
    wanted = {
        "chevron_down": ("chevron-down", TOKENS["muted"], 12, 2.5),
        "chevron_up": ("chevron-up", TOKENS["muted"], 10, 2.5),
        "chevron_down_small": ("chevron-down", TOKENS["muted"], 10, 2.5),
        "chevron_down_off": ("chevron-down", TOKENS["faint"], 12, 2.5),
        "check": ("check", TOKENS["on_accent"], 12, 3.5),
    }
    out: dict[str, str] = {}
    for key, (name, tint, size, stroke) in wanted.items():
        base = folder / f"{key}.png"
        pixmap(name, tint, size, stroke=stroke).save(str(base))
        pixmap(name, tint, size, scale=2.0, stroke=stroke).save(
            str(folder / f"{key}@2x.png"))
        out[key] = base.as_posix()
    return out


def style_sheet(assets: dict[str, str] | None = None) -> str:
    t = dict(TOKENS)
    a = assets or {}
    r = RADIUS

    def img(key: str) -> str:
        return f"image: url({a[key]});" if key in a else ""

    return f"""
QMainWindow {{ background: {t['bg']}; }}
QMainWindow::separator {{ background: {t['bg']}; width: 5px; height: 5px; }}
QMainWindow::separator:hover {{ background: {t['accent_soft']}; }}
QSplitter::handle {{ background: {t['bg']}; }}
QSplitter::handle:hover {{ background: {t['accent_soft']}; }}

QToolTip {{
    background: {t['raised']}; color: {t['text']};
    border: 1px solid {t['border_strong']}; border-radius: 4px; padding: 4px 8px;
}}

/* -- labels by role ---------------------------------------------------- */
QLabel[role="hint"] {{ color: {t['muted']}; }}
QLabel[role="faint"] {{ color: {t['faint']}; }}
QLabel[role="warning"] {{ color: {t['warning']}; }}
QLabel[role="error"] {{ color: {t['danger']}; }}
QLabel[role="success"] {{ color: {t['success']}; }}
QLabel[role="title"] {{ font-size: 26px; font-weight: 600; color: {t['text']}; }}
QLabel[role="subtitle"] {{ font-size: 14px; color: {t['muted']}; }}
QLabel[role="section"] {{
    font-size: 11px; font-weight: 700; color: {t['muted']}; letter-spacing: 0.6px;
}}
QLabel[role="dock-title"] {{
    font-size: 11px; font-weight: 700; color: {t['muted']}; letter-spacing: 0.8px;
}}
QLabel[role="kbd"] {{
    color: {t['muted']}; background: {t['raised']}; border: 1px solid {t['border_strong']};
    border-radius: 4px; padding: 1px 6px; font-size: 11px;
}}
QFrame[role="card"] {{
    background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 10px;
}}
QFrame[role="card"] QLabel {{ background: transparent; }}
QWidget[role="dock-bar"] {{ background: {t['bg']}; border-bottom: 1px solid {t['border']}; }}
QWidget[role="page"] {{ background: {t['bg']}; }}

/* -- buttons ------------------------------------------------------------ */
QPushButton {{
    background: {t['raised']}; color: {t['text']};
    border: 1px solid {t['border_strong']}; border-radius: {r}px;
    padding: 5px 14px; min-height: 18px;
}}
QPushButton:hover {{ background: {t['hover']}; }}
QPushButton:pressed {{ background: {t['border_strong']}; }}
QPushButton:checked {{ background: {t['accent_soft']}; border-color: {t['accent']}; }}
QPushButton:disabled {{
    color: {t['faint']}; background: {t['panel']}; border-color: {t['border']};
}}
QPushButton:default, QPushButton[primary="true"] {{
    background: {t['accent']}; color: {t['on_accent']}; border-color: {t['accent']};
    font-weight: 600;
}}
QPushButton:default:hover, QPushButton[primary="true"]:hover {{
    background: {t['accent_hover']}; border-color: {t['accent_hover']};
}}
QPushButton:default:pressed, QPushButton[primary="true"]:pressed {{
    background: {t['accent_press']};
}}
QPushButton:default:disabled, QPushButton[primary="true"]:disabled {{
    background: {t['accent_soft']}; color: {t['muted']}; border-color: {t['accent_soft']};
}}
QPushButton[flat="true"] {{ background: transparent; border-color: transparent; }}
QPushButton[flat="true"]:hover {{ background: {t['raised']}; }}
QPushButton[role="link"] {{
    background: transparent; border: none; color: {t['accent']};
    padding: 2px 0; text-align: left;
}}
QPushButton[role="link"]:hover {{ color: {t['accent_hover']}; text-decoration: underline; }}

/* -- toolbar ------------------------------------------------------------ */
QToolBar {{
    background: {t['bg']}; border: none; border-bottom: 1px solid {t['border']};
    padding: 4px 8px; spacing: 2px;
}}
QToolBar::separator {{
    background: {t['border_strong']}; width: 1px; margin: 6px 8px;
}}
QToolButton {{
    background: transparent; color: {t['text']}; border: 1px solid transparent;
    border-radius: {r}px; padding: 4px 8px;
}}
QToolButton:hover {{ background: {t['raised']}; border-color: {t['border']}; }}
QToolButton:pressed {{ background: {t['hover']}; }}
QToolButton:checked {{ background: {t['accent_soft']}; border-color: {t['accent']}; }}
QToolButton:disabled {{ color: {t['faint']}; }}
QToolButton[primary="true"] {{
    background: {t['accent']}; color: {t['on_accent']}; border-color: {t['accent']};
    font-weight: 600;
}}
QToolButton[primary="true"]:hover {{ background: {t['accent_hover']}; }}
QToolButton[primary="true"]:pressed {{ background: {t['accent_press']}; }}
QToolButton[primary="true"]:disabled {{
    background: {t['accent_soft']}; color: {t['muted']}; border-color: {t['accent_soft']};
}}
QToolButton::menu-indicator {{ image: none; }}
QToolBar QToolButton {{ font-size: {FONT_PX}px; padding: 5px 9px; }}

/* -- inputs ------------------------------------------------------------- */
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {t['surface']}; color: {t['text']};
    border: 1px solid {t['border']}; border-radius: {r}px;
    padding: 3px 7px; selection-background-color: {t['accent']};
    selection-color: {t['on_accent']};
}}
QPlainTextEdit, QTextEdit {{ padding: 4px; }}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
    border-color: {t['border_strong']};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {t['accent']}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QPlainTextEdit:disabled, QTextEdit:disabled {{
    color: {t['faint']}; background: {t['panel']}; border-color: {t['border']};
}}
QComboBox {{ padding-right: 24px; }}
QComboBox::drop-down {{
    subcontrol-origin: padding; subcontrol-position: center right;
    width: 22px; border: none;
}}
QComboBox::down-arrow {{ {img('chevron_down')} width: 12px; height: 12px; }}
QComboBox::down-arrow:disabled {{ {img('chevron_down_off')} }}
QComboBox QAbstractItemView {{
    background: {t['surface']}; color: {t['text']};
    border: 1px solid {t['border_strong']}; padding: 4px; outline: 0;
    selection-background-color: {t['accent_soft']}; selection-color: {t['text']};
}}
QSpinBox, QDoubleSpinBox {{ padding-right: 18px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border; subcontrol-position: top right;
    width: 18px; border: none; background: transparent; margin: 2px 2px 0 0;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border; subcontrol-position: bottom right;
    width: 18px; border: none; background: transparent; margin: 0 2px 2px 0;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {t['hover']}; border-radius: 3px;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ {img('chevron_up')} width: 10px; height: 10px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    {img('chevron_down_small')} width: 10px; height: 10px;
}}

/* -- check boxes / radios ---------------------------------------------- */
QCheckBox, QRadioButton {{ spacing: 8px; color: {t['text']}; background: transparent; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {t['faint']}; }}
QCheckBox::indicator, QAbstractItemView::indicator, QGroupBox::indicator {{
    width: 14px; height: 14px; border: 1px solid {t['border_strong']};
    border-radius: 4px; background: {t['surface']};
}}
QCheckBox::indicator:hover, QAbstractItemView::indicator:hover {{
    border-color: {t['accent']};
}}
QCheckBox::indicator:checked, QAbstractItemView::indicator:checked,
QGroupBox::indicator:checked {{
    background: {t['accent']}; border-color: {t['accent']}; {img('check')}
}}
QCheckBox::indicator:disabled, QAbstractItemView::indicator:disabled {{
    background: {t['panel']}; border-color: {t['border']};
}}
QCheckBox::indicator:checked:disabled, QAbstractItemView::indicator:checked:disabled {{
    background: {t['border_strong']}; border-color: {t['border_strong']};
}}
QRadioButton::indicator {{
    width: 14px; height: 14px; border: 1px solid {t['border_strong']};
    border-radius: 8px; background: {t['surface']};
}}
QRadioButton::indicator:hover {{ border-color: {t['accent']}; }}
QRadioButton::indicator:checked {{
    border-color: {t['accent']};
    background: qradialgradient(cx: 0.5, cy: 0.5, radius: 0.5, fx: 0.5, fy: 0.5,
        stop: 0 {t['on_accent']}, stop: 0.38 {t['on_accent']},
        stop: 0.48 {t['accent']}, stop: 1 {t['accent']});
}}
QRadioButton::indicator:disabled {{ background: {t['panel']}; border-color: {t['border']}; }}

/* -- group boxes -------------------------------------------------------- */
QGroupBox {{
    border: 1px solid {t['border']}; border-radius: 8px;
    margin-top: 18px; padding: 10px 8px 8px 8px; background: transparent;
}}
QGroupBox::title {{
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 2px; top: 0px; padding: 0 2px; color: {t['muted']}; font-weight: 600;
}}

/* -- tabs --------------------------------------------------------------- */
QTabWidget::pane {{ border: none; border-top: 1px solid {t['border']}; top: -1px; }}
QTabBar {{ background: transparent; qproperty-drawBase: 0; }}
QTabBar::tab {{
    background: transparent; color: {t['muted']};
    border: none; border-bottom: 2px solid transparent;
    padding: 7px 10px; margin-right: 2px;
}}
QTabBar::tab:hover:!selected {{ color: {t['text']}; }}
QTabBar::tab:selected {{ color: {t['text']}; border-bottom-color: {t['accent']}; }}
QTabBar::tab:disabled {{ color: {t['faint']}; }}
QTabBar QToolButton {{
    background: {t['panel']}; border: 1px solid {t['border']}; border-radius: 4px;
    padding: 2px;
}}

/* -- item views --------------------------------------------------------- */
QTreeView, QTreeWidget, QListView, QListWidget, QTableView, QTableWidget {{
    background: {t['panel']}; alternate-background-color: {t['surface']};
    color: {t['text']}; border: none; outline: 0;
    selection-background-color: {t['accent_soft']}; selection-color: {t['text']};
}}
QTreeView::item, QListView::item, QTableView::item {{ padding: 2px 4px; }}
QTreeView::item:hover, QListView::item:hover, QTableView::item:hover {{
    background: {t['surface']};
}}
QTreeView::item:selected, QListView::item:selected, QTableView::item:selected {{
    background: {t['accent_soft']}; color: {t['text']};
}}
QTreeView::branch:selected {{ background: {t['accent_soft']}; }}
QHeaderView {{ background: {t['panel']}; border: none; }}
QHeaderView::section {{
    background: {t['panel']}; color: {t['muted']}; font-weight: 600;
    border: none; border-bottom: 1px solid {t['border']};
    border-right: 1px solid {t['border']}; padding: 4px 8px;
}}
QHeaderView::section:last {{ border-right: none; }}
QTableCornerButton::section {{ background: {t['panel']}; border: none; }}

/* -- scroll bars -------------------------------------------------------- */
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget#qt_scrollarea_viewport {{ background: transparent; }}
QScrollArea > QWidget#qt_scrollarea_viewport > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {t['border_strong']}; border-radius: 3px; min-height: 28px; margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {t['border_strong']}; border-radius: 3px; min-width: 28px; margin: 2px;
}}
QScrollBar::handle:hover {{ background: {t['faint']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

/* -- slider / progress -------------------------------------------------- */
QSlider::groove:horizontal {{
    height: 4px; background: {t['border_strong']}; border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {t['accent']}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {t['text']}; width: 14px; height: 14px; margin: -5px 0;
    border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{ background: {t['on_accent']}; }}
QSlider::sub-page:horizontal:disabled {{ background: {t['border_strong']}; }}
QSlider::handle:horizontal:disabled {{ background: {t['faint']}; }}
QProgressBar {{
    background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 4px;
    color: {t['muted']}; text-align: center; max-height: 14px; font-size: 11px;
}}
QProgressBar::chunk {{ background: {t['accent']}; border-radius: 3px; }}

/* -- menus / status bar ------------------------------------------------- */
QMenuBar {{ background: {t['bg']}; color: {t['text']}; }}
QMenuBar::item {{ background: transparent; padding: 4px 10px; border-radius: 4px; }}
QMenuBar::item:selected {{ background: {t['raised']}; }}
QMenu {{
    background: {t['surface']}; color: {t['text']};
    border: 1px solid {t['border_strong']}; padding: 4px;
}}
QMenu::item {{ padding: 6px 28px 6px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {t['accent_soft']}; }}
QMenu::item:disabled {{ color: {t['faint']}; }}
QMenu::separator {{ height: 1px; background: {t['border']}; margin: 4px 8px; }}
QMenu::icon {{ padding-left: 8px; }}
QStatusBar {{
    background: {t['bg']}; color: {t['muted']}; border-top: 1px solid {t['border']};
}}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {t['muted']}; padding: 0 6px; }}

/* -- docks -------------------------------------------------------------- */
QDockWidget {{ color: {t['muted']}; }}
QDockWidget > QWidget {{ background: {t['panel']}; }}
"""


def apply(app: QApplication) -> None:
    """Fusion + the dark palette + the style sheet + the UI font size."""
    app.setStyle("Fusion")
    app.setPalette(_palette())
    font = QFont(app.font())
    font.setPixelSize(FONT_PX)
    app.setFont(font)
    app.setStyleSheet(style_sheet(_assets()))


def set_role(widget: QWidget, role: str) -> QWidget:
    """Mark ``widget`` with a style-sheet role (re-polished if already shown)."""
    widget.setProperty("role", role)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    return widget


def set_primary(widget: QWidget, primary: bool = True) -> QWidget:
    widget.setProperty("primary", primary)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    return widget


__all__ = ["FONT_PX", "RADIUS", "TOKENS", "apply", "color", "set_primary", "set_role",
           "style_sheet"]
