"""Asset ▸ Compare with original: a swap-hands result beside its source, frame
by frame, as the player sees them (:mod:`valve_qc_merger.preview.compare`),
with the retarget check's verdict above."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QWidget,
)

from valve_qc_merger.studio import dialog_kit as kit
from valve_qc_merger.studio.sprite_panel import rgba_pixmap


class CompareDialog(QDialog):
    """The original's frames on top, the retarget's below."""

    def __init__(self, original: Path, result: Path, title: str,
                 audit: dict | None = None, parent: QWidget | None = None) -> None:
        from valve_qc_merger.preview.compare import sequences_in_both
        super().__init__(parent)
        self.original, self.result = Path(original), Path(result)
        self.setWindowTitle(f"Compare — {title}")
        self.resize(1100, 640)
        layout = kit.dialog_layout(self)
        layout.addWidget(kit.header(
            f"{title} beside its original",
            "Top: the original with its own hands; below: on our hands. Seen as in the "
            "game — eye at the origin, a widescreen view — so an arm end showing on screen "
            "shows here too."))
        for line in (audit or {}).get("problems", []):
            layout.addWidget(kit.hint(f"✕ {line}"))
        for line in (audit or {}).get("warnings", []):
            layout.addWidget(kit.hint(f"▲ {line}"))
        if audit and not audit.get("problems") and not audit.get("warnings") \
                and "note" not in audit:
            layout.addWidget(kit.hint(
                f"Retarget check: weapon in place, hands {audit.get('fit', 0):.2f} u off the "
                "original's on average, no jumps, no shoulder in view."))
        controls = QWidget()
        row = QHBoxLayout(controls)
        row.setContentsMargins(0, 0, 0, 0)
        self.sequence_box = QComboBox()
        self.sequence_box.addItems(sequences_in_both(self.original, self.result))
        self.frames_spin = kit.number_spin(QSpinBox())
        self.frames_spin.setRange(1, 10)
        self.frames_spin.setValue(5)
        show = QPushButton("Show")
        show.clicked.connect(self.render)
        self.sequence_box.currentIndexChanged.connect(lambda _i: self.render())
        row.addWidget(QLabel("Sequence"))
        row.addWidget(self.sequence_box, 1)
        row.addWidget(QLabel("Frames"))
        row.addWidget(self.frames_spin)
        row.addWidget(show)
        layout.addWidget(controls)
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll = QScrollArea()
        scroll.setWidget(self.picture)
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if self.sequence_box.count():
            self.render()

    def render(self) -> None:
        from valve_qc_merger.preview.compare import compare_sheet
        name = self.sequence_box.currentText()
        if not name:
            return
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            sheet = compare_sheet(self.original, self.result, name,
                                  frames=self.frames_spin.value())
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.picture.setPixmap(rgba_pixmap(sheet))


__all__ = ["CompareDialog"]
