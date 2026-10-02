"""The 3D viewport widget and its control bar."""

from __future__ import annotations

import time

from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.studio.renderer import Renderer, ViewState, gl_format
from valve_qc_merger.studio.scene import ModelScene

SPEEDS = [0.1, 0.25, 0.5, 1.0, 2.0]


class Viewport(QOpenGLWidget):
    """Orbit (left drag), pan (right/middle drag), zoom (wheel), frame
    (double click). Any drag leaves the first-person camera."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFormat(gl_format())
        self.setMinimumSize(320, 240)
        self.renderer = Renderer()
        self.state = ViewState()
        self.scene: ModelScene | None = None
        self._last: QPointF | None = None
        self.gl_error = ""

    # -- GL ----------------------------------------------------------------
    def initializeGL(self) -> None:  # noqa: N802 - Qt override
        try:
            self.renderer.initialize()
        except Exception as exc:  # noqa: BLE001 - shown in the widget instead
            self.gl_error = str(exc)
            return
        # free buffers/textures while the context still exists
        self.context().aboutToBeDestroyed.connect(self._cleanup)

    def _cleanup(self) -> None:
        self.makeCurrent()
        self.renderer.shutdown()
        self.doneCurrent()

    def paintGL(self) -> None:  # noqa: N802 - Qt override
        if self.gl_error:
            return
        ratio = self.devicePixelRatioF()
        self.renderer.render(int(self.width() * ratio), int(self.height() * ratio), self.state)

    # -- scene -------------------------------------------------------------
    def set_scene(self, scene: ModelScene | None) -> None:
        self.scene = scene
        self.makeCurrent()
        self.renderer.set_scene(scene)
        self.doneCurrent()
        self.state.frame = 0.0
        self.state.sequence = 0 if scene is not None and scene.sequences else None
        self.state.bodygroups = {}
        if scene is not None:
            self.frame_model()
        self.update()

    def frame_model(self) -> None:
        if self.scene is not None:
            self.state.camera.frame(*self.scene.bounds())
            self.update()

    def first_person(self) -> None:
        self.state.camera.first_person = True
        self.update()

    # -- mouse -------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._last = event.position()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._last = None

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.frame_model()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._last is None:
            return
        delta = event.position() - self._last
        self._last = event.position()
        camera = self.state.camera
        camera.first_person = False
        if event.buttons() & Qt.MouseButton.LeftButton:
            camera.yaw -= delta.x() * 0.4
            camera.pitch = max(-89.0, min(89.0, camera.pitch + delta.y() * 0.4))
        elif event.buttons() & (Qt.MouseButton.RightButton | Qt.MouseButton.MiddleButton):
            import math
            scale = camera.distance * 0.0016
            yaw = math.radians(camera.yaw)
            right = (-math.sin(yaw), math.cos(yaw), 0.0)
            tx, ty, tz = camera.target
            camera.target = (tx - right[0] * delta.x() * scale,
                             ty - right[1] * delta.x() * scale,
                             tz + delta.y() * scale)
        self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        camera = self.state.camera
        camera.first_person = False
        steps = event.angleDelta().y() / 120.0
        camera.distance = max(1.0, camera.distance * (0.88 ** steps))
        self.update()


class ViewportPanel(QWidget):
    """Viewport plus playback, bodygroup and display controls."""

    frame_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.viewport = Viewport()
        self.sequence_box = QComboBox()
        self.sequence_box.setMinimumWidth(200)
        self.sequence_box.currentIndexChanged.connect(self._sequence_changed)
        self.play_button = QPushButton("▶")
        self.play_button.setFixedWidth(36)
        self.play_button.setCheckable(True)
        self.play_button.toggled.connect(self._toggle_play)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.valueChanged.connect(self._slider_moved)
        self.frame_label = QLabel("0 / 0")
        self.frame_label.setMinimumWidth(70)
        self.speed_box = QComboBox()
        for speed in SPEEDS:
            self.speed_box.addItem(f"{speed:g}×", speed)
        self.speed_box.setCurrentIndex(SPEEDS.index(1.0))

        top = QHBoxLayout()
        top.addWidget(QLabel("Sequence"))
        top.addWidget(self.sequence_box)
        top.addWidget(self.play_button)
        top.addWidget(self.slider, 1)
        top.addWidget(self.frame_label)
        top.addWidget(self.speed_box)

        self.groups_row = QHBoxLayout()
        self.group_boxes: dict[str, QComboBox] = {}
        bottom = QHBoxLayout()
        bottom.addLayout(self.groups_row)
        bottom.addStretch(1)
        self.textures_box = self._toggle("Textures", True, "textured")
        self.bones_box = self._toggle("Bones", False, "show_bones")
        self.attach_box = self._toggle("Attachments", True, "show_attachments")
        self.wire_box = self._toggle("Wireframe", False, "wireframe")
        for box in (self.textures_box, self.bones_box, self.attach_box, self.wire_box):
            bottom.addWidget(box)
        frame_button = QPushButton("Frame")
        frame_button.clicked.connect(self.viewport.frame_model)
        fp_button = QPushButton("First person")
        fp_button.clicked.connect(self.viewport.first_person)
        bottom.addWidget(frame_button)
        bottom.addWidget(fp_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(top)
        layout.addWidget(self.viewport, 1)
        layout.addLayout(bottom)

        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)
        self._clock = time.monotonic()
        self.set_scene(None)

    def _toggle(self, text: str, value: bool, attr: str) -> QCheckBox:
        box = QCheckBox(text)
        box.setChecked(value)
        setattr(self.viewport.state, attr, value)
        box.toggled.connect(lambda on: (setattr(self.viewport.state, attr, on),
                                        self.viewport.update()))
        return box

    # -- scene -------------------------------------------------------------
    def set_scene(self, scene: ModelScene | None) -> None:
        self.play_button.setChecked(False)
        self.viewport.set_scene(scene)
        self.sequence_box.blockSignals(True)
        self.sequence_box.clear()
        if scene is not None:
            for i, seq in enumerate(scene.sequences):
                self.sequence_box.addItem(f"{i}: {seq.name}", i)
        self.sequence_box.blockSignals(False)
        while self.groups_row.count():
            item = self.groups_row.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.group_boxes.clear()
        if scene is not None:
            for group, entries in scene.groups.items():
                if len(entries) < 2:
                    continue
                label = QLabel(group)
                box = QComboBox()
                for entry in entries:
                    box.addItem(entry)
                box.currentIndexChanged.connect(
                    lambda index, g=group: self._group_changed(g, index))
                self.groups_row.addWidget(label)
                self.groups_row.addWidget(box)
                self.group_boxes[group] = box
        has = scene is not None and bool(scene.sequences)
        for widget in (self.sequence_box, self.play_button, self.slider, self.speed_box):
            widget.setEnabled(has)
        self._sequence_changed(0)

    def _group_changed(self, group: str, index: int) -> None:
        self.viewport.state.bodygroups[group] = index
        self.viewport.update()

    def _sequence_changed(self, index: int) -> None:
        scene = self.viewport.scene
        if scene is None or not scene.sequences or index < 0:
            self.slider.setRange(0, 0)
            self.frame_label.setText("0 / 0")
            return
        self.viewport.state.sequence = index
        self.viewport.state.frame = 0.0
        frames = scene.sequences[index].frames
        self.slider.blockSignals(True)
        self.slider.setRange(0, max(frames - 1, 0))
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self._update_label()
        self.viewport.update()

    def _slider_moved(self, value: int) -> None:
        self.viewport.state.frame = float(value)
        self._update_label()
        self.viewport.update()

    def _toggle_play(self, playing: bool) -> None:
        self.play_button.setText("⏸" if playing else "▶")
        self._clock = time.monotonic()
        if playing:
            self.timer.start()
        else:
            self.timer.stop()

    def _tick(self) -> None:
        scene = self.viewport.scene
        state = self.viewport.state
        if scene is None or state.sequence is None:
            return
        now = time.monotonic()
        elapsed, self._clock = now - self._clock, now
        seq = scene.sequences[state.sequence]
        state.frame += elapsed * seq.fps * self.speed_box.currentData()
        if seq.loop:
            state.frame %= max(seq.frames, 1)
        elif state.frame >= seq.frames - 1:
            state.frame = 0.0  # non-looping sequences replay from the start
        self.slider.blockSignals(True)
        self.slider.setValue(int(state.frame))
        self.slider.blockSignals(False)
        self._update_label()
        self.viewport.update()

    def _update_label(self) -> None:
        scene = self.viewport.scene
        state = self.viewport.state
        if scene is None or state.sequence is None:
            self.frame_label.setText("0 / 0")
            return
        total = scene.sequences[state.sequence].frames
        self.frame_label.setText(f"{int(state.frame)} / {total - 1}")


__all__ = ["Viewport", "ViewportPanel"]
