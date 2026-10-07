"""Run a service off the UI thread; stream its log/progress back as signals.

One job at a time: the window disables project-changing actions while
:attr:`JobRunner.busy`. Cancelling sets the reporter's event; the service
raises :class:`~valve_qc_merger.services.base.Cancelled` at its next check.

Heavy work (merges, retargets, imports) runs in a **process**
(:meth:`JobRunner.start_process`, a task of :mod:`.tasks`): a thread shares
the interpreter lock with the window, and a pure-Python merge holding it
makes every repaint wait — the window stalls. The process reopens the
project from disk, streams its log and progress through a queue the window
polls, and hands back a picklable result; the window then re-reads the
project. Light jobs keep using a thread (:meth:`JobRunner.start`).
"""

from __future__ import annotations

import multiprocessing
import queue as queue_module
import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, QThread, QTimer, Signal

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


def _child(task: str, kwargs: dict[str, Any], channel: Any, cancel: Any) -> None:
    """The job process: run ``task`` of :mod:`.tasks`, report through
    ``channel`` (log / progress / finished / failed messages)."""
    from valve_qc_merger.studio import tasks
    reporter = CallbackReporter(
        on_log=lambda message: channel.put(("log", message)),
        on_progress=lambda done, total, label: channel.put(("progress", done, total, label)),
        cancel=cancel,
    )
    try:
        result = tasks.run(task, kwargs, reporter)
    except Cancelled:
        channel.put(("failed", "cancelled"))
    except Exception as exc:  # noqa: BLE001 - shown to the user, not swallowed
        channel.put(("log", traceback.format_exc().rstrip()))
        channel.put(("failed", f"{type(exc).__name__}: {exc}"))
    else:
        try:
            channel.put(("finished", result))
        except Exception as exc:  # noqa: BLE001 - an unpicklable result
            channel.put(("failed", f"result could not be sent back: {exc}"))


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
        self._process: Any = None  # multiprocessing.Process of a process job
        self._channel: Any = None
        self._process_cancel: Any = None
        self._poller = QTimer(self)
        self._poller.setInterval(50)
        self._poller.timeout.connect(self._poll)
        self.process_job = False  # the last job ran in a process (the window re-reads)

    @property
    def busy(self) -> bool:
        return self._thread is not None or self._process is not None

    def start(self, title: str, work: Callable[[Reporter], Any]) -> bool:
        if self.busy:
            return False
        self._title = title
        self._cancel = threading.Event()
        self.process_job = False
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

    def start_process(self, title: str, task: str, **kwargs: Any) -> bool:
        """Run ``tasks.run(task, kwargs)`` in a process of its own."""
        if self.busy:
            return False
        context = multiprocessing.get_context("spawn")
        self._title = title
        self._channel = context.Queue()
        self._process_cancel = context.Event()
        self._process = context.Process(
            target=_child, args=(task, kwargs, self._channel, self._process_cancel),
            daemon=True, name=f"vqm-{task}")
        self.process_job = True
        self.started.emit(title)
        self._process.start()
        self._poller.start()
        return True

    def _poll(self) -> None:
        """Forward the process's messages; finish on its last one."""
        if self._process is None:
            self._poller.stop()
            return
        for _ in range(500):  # bounded: the window stays responsive under a flood
            try:
                message = self._channel.get_nowait()
            except (queue_module.Empty, OSError, EOFError):
                break
            kind = message[0]
            if kind == "log":
                self.log.emit(message[1])
            elif kind == "progress":
                self.progress.emit(*message[1:])
            elif kind == "finished":
                self._end_process(True, message[1])
                return
            elif kind == "failed":
                self._end_process(False, message[1])
                return
        if not self._process.is_alive():
            try:  # a last message may still be in flight
                message = self._channel.get(timeout=0.2)
            except (queue_module.Empty, OSError, EOFError):
                code = self._process.exitcode
                self._end_process(False, "cancelled" if self._process_cancel.is_set()
                                  else f"the job's process ended unexpectedly (exit {code})")
                return
            self._channel_put_back(message)

    def _channel_put_back(self, message: tuple) -> None:
        kind = message[0]
        if kind == "finished":
            self._end_process(True, message[1])
        elif kind == "failed":
            self._end_process(False, message[1])
        elif kind == "log":
            self.log.emit(message[1])
        elif kind == "progress":
            self.progress.emit(*message[1:])

    def _end_process(self, ok: bool, payload: object) -> None:
        self._poller.stop()
        process, title = self._process, self._title
        self._process = None
        if process is not None:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
        self.done.emit(title, ok, payload)

    def cancel(self) -> None:
        self._cancel.set()
        if self._process is not None and self._process_cancel is not None:
            self._process_cancel.set()
            # a service checks between units; one stuck in a long unit is stopped
            QTimer.singleShot(15_000, self._terminate_if_stuck)

    def _terminate_if_stuck(self) -> None:
        if self._process is not None and self._process.is_alive():
            self._process.terminate()

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
        self.process_job = False
        thread, title = self._thread, self._title
        self._thread = self._worker = None
        if thread is not None:
            thread.quit()
            thread.wait()
        self.done.emit(title, ok, payload)


__all__ = ["JobRunner"]
