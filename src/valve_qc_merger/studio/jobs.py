"""Run a service off the UI thread; stream its log/progress back as signals.

One job at a time: the window disables project-changing actions while
:attr:`JobRunner.busy`. Cancelling sets the reporter's event; the service
raises :class:`~valve_qc_merger.services.base.Cancelled` at its next check.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, QThread, Signal

from valve_qc_merger.services.base import CallbackReporter, Cancelled, Reporter


class _Worker(QObject):
    log = Signal(str)
    progress = Signal(int, int, str)
    finished = Signal(object)  # the callable's return value
    failed = Signal(str)  # formatted error (cancellation included)

    def __init__(self, work: Callable[[Reporter], Any], cancel: threading.Event) -> None:
        super().__init__()
        self._work = work
        self._cancel = cancel

    def run(self) -> None:
        reporter = CallbackReporter(
            on_log=self.log.emit,
            on_progress=lambda d, t, label: self.progress.emit(d, t, label),
            cancel=self._cancel,
        )
        try:
            result = self._work(reporter)
        except Cancelled:
            self.failed.emit("cancelled")
        except Exception as exc:  # noqa: BLE001 - shown to the user, not swallowed
            self.log.emit(traceback.format_exc().rstrip())
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.finished.emit(result)


class JobRunner(QObject):
    """Owns the worker thread of the single running job."""

    started = Signal(str)  # job title
    log = Signal(str)
    progress = Signal(int, int, str)
    done = Signal(str, bool, object)  # title, ok, result-or-error

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: _Worker | None = None
        self._cancel = threading.Event()
        self._title = ""

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def start(self, title: str, work: Callable[[Reporter], Any]) -> bool:
        if self.busy:
            return False
        self._title = title
        self._cancel = threading.Event()
        thread = QThread()
        worker = _Worker(work, self._cancel)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.log.connect(self.log)
        worker.progress.connect(self.progress)
        # Bound methods of this (main-thread) object: Qt queues the call to
        # the UI thread. A lambda would run in the worker thread and then
        # wait() on its own thread.
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        self._thread, self._worker = thread, worker
        self.started.emit(title)
        thread.start()
        return True

    def cancel(self) -> None:
        self._cancel.set()

    def wait(self, msecs: int = 60_000) -> bool:
        """Pump the UI event loop until the job has ended (tests, shutdown).

        Blocking on the thread would deadlock: completion is delivered to
        this (UI) thread as a queued call. Returns False on timeout.
        """
        deadline = time.monotonic() + msecs / 1000
        while self.busy and time.monotonic() < deadline:
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        return not self.busy

    def _on_finished(self, result: object) -> None:
        self._finish(True, result)

    def _on_failed(self, error: str) -> None:
        self._finish(False, error)

    def _finish(self, ok: bool, payload: object) -> None:
        thread, title = self._thread, self._title
        self._thread = self._worker = None
        if thread is not None:
            thread.quit()
            thread.wait()
        self.done.emit(title, ok, payload)


__all__ = ["JobRunner"]
