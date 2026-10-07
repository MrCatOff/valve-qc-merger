"""Heavy studio jobs run in a process of their own."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture
def runner():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from valve_qc_merger.studio.jobs import JobRunner
    jobs = JobRunner()
    events: list[tuple] = []
    jobs.log.connect(lambda line: events.append(("log", line)))
    jobs.progress.connect(lambda d, t, label: events.append(("progress", d, t, label)))
    jobs.done.connect(lambda title, ok, payload: events.append(("done", title, ok, payload)))
    yield jobs, events
    jobs.cancel()
    jobs.wait(10_000)


def test_process_job_streams_and_returns(runner, tmp_path: Path) -> None:
    jobs, events = runner
    assert jobs.start_process("Ping", "ping", root=str(tmp_path / "pack"))
    assert jobs.busy and not jobs.start_process("again", "ping", root=".")  # one at a time
    assert jobs.wait(60_000) and not jobs.busy and jobs.process_job
    assert ("log", "job process alive") in events
    assert ("progress", 1, 1, "ping") in events
    assert events[-1] == ("done", "Ping", True, "pong from pack")


def test_process_job_failure_and_cancel(runner, tmp_path: Path) -> None:
    jobs, events = runner
    jobs.start_process("Broken", "no-such-task", root=str(tmp_path))
    assert jobs.wait(60_000)
    assert events[-1][:3] == ("done", "Broken", False) and "KeyError" in events[-1][3]
    assert any("Traceback" in e[1] for e in events if e[0] == "log")
    jobs.start_process("Long", "ping", root=str(tmp_path), seconds=30)
    jobs.wait(1_500)  # let it start
    jobs.cancel()
    assert jobs.wait(20_000)
    assert events[-1] == ("done", "Long", False, "cancelled")


def test_thread_jobs_still_work(runner) -> None:
    jobs, events = runner
    jobs.start("Light", lambda reporter: reporter.log("in a thread") or 7)
    assert jobs.wait(10_000) and not jobs.process_job
    assert ("log", "in a thread") in events and events[-1] == ("done", "Light", True, 7)
