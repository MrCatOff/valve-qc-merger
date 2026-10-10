"""Worker processes / threads: results in order, failures as results, cancel."""

from __future__ import annotations

import threading

import pytest

from valve_qc_merger import workers
from valve_qc_merger.services.base import CallbackReporter, Cancelled, CollectingReporter


def _square(value: int, *, reporter) -> int:  # noqa: ANN001
    reporter.log(f"square {value}")
    if value == 3:
        raise ValueError("three")
    return value * value


@pytest.mark.parametrize("count", [1, 3])
def test_processes_keep_order_and_turn_failures_into_results(count: int) -> None:
    reporter = CollectingReporter()
    results = workers.processes([(_square, (i,)) for i in range(5)], workers=count,
                                reporter=reporter, label="squares")
    assert results[:3] == [0, 1, 4] and results[4] == 16
    assert isinstance(results[3], ValueError)
    assert "square 4" in reporter.lines and any("three" in line for line in reporter.lines)


def test_threads_and_cancel() -> None:
    assert workers.threads([(_square, (i,)) for i in (1, 2)], workers=2,
                           reporter=CollectingReporter()) == [1, 4]
    stop = threading.Event()
    stop.set()
    with pytest.raises(Cancelled):
        workers.threads([(_square, (i,)) for i in range(4)], workers=2,
                        reporter=CallbackReporter(cancel=stop))


def test_count() -> None:
    assert workers.count(4) == 4
    assert workers.count(0) == workers.default_workers() >= 1
