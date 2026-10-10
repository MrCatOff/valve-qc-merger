"""Run independent heavy steps — a retarget per model, a decompile per .mdl,
a QC per studiomdl — on several cores.

Retarget, decompile and normalisation are pure Python and CPU-bound: threads
would share one interpreter lock, so they run in worker PROCESSES
(``spawn``: the same in the frozen exe, which calls ``freeze_support``).
studiomdl is a process of its own already: :func:`threads` runs several at
once from threads.

Workers only compute and write into their own folders; the caller changes
the project (``project.toml``) from the one process that owns it. Each task
logs into a buffer the caller prints when the task ends, so one task's lines
stay together. Cancel stops what has not started and ends the running
workers.
"""

from __future__ import annotations

import multiprocessing
import os
from collections.abc import Callable, Sequence
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from typing import Any

from valve_qc_merger.services.base import Cancelled, CollectingReporter, Reporter

Task = tuple[Callable[..., Any], tuple[Any, ...]]


def default_workers() -> int:
    """Cores minus one (the window / main process keeps one), at least 1."""
    return max(1, (os.cpu_count() or 2) - 1)


def count(setting: int | None) -> int:
    """Workers to use: ``setting`` when positive, else :func:`default_workers`."""
    return setting if setting and setting > 0 else default_workers()


def _call(fn: Callable[..., Any], args: tuple[Any, ...]) -> tuple[Any, list[str]]:
    """In a worker: run ``fn(*args, reporter=…)`` with a collecting reporter."""
    reporter = CollectingReporter()
    try:
        return fn(*args, reporter=reporter), reporter.lines
    except Exception as exc:  # noqa: BLE001 - one task's failure is its result
        reporter.log(f"error: {type(exc).__name__}: {exc}")
        return exc, reporter.lines


def processes(tasks: Sequence[Task], *, workers: int, reporter: Reporter,
              label: str = "") -> list[Any]:
    """Run every ``fn(*args, reporter=…)`` of ``tasks`` in worker processes;
    returns their results in task order (an exception object where a task
    raised). One worker (or one task): in this process."""
    return _run(tasks, workers=workers, reporter=reporter, label=label, threaded=False)


def threads(tasks: Sequence[Task], *, workers: int, reporter: Reporter,
            label: str = "") -> list[Any]:
    """:func:`processes` with threads — for steps that run an external
    program (studiomdl), which releases the interpreter lock."""
    return _run(tasks, workers=workers, reporter=reporter, label=label, threaded=True)


def _run(tasks: Sequence[Task], *, workers: int, reporter: Reporter, label: str,
         threaded: bool) -> list[Any]:
    results: list[Any] = [None] * len(tasks)
    if workers <= 1 or len(tasks) <= 1:
        for index, (fn, args) in enumerate(tasks):
            reporter.check()
            reporter.progress(index, len(tasks), label)
            try:
                results[index] = fn(*args, reporter=reporter)
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - as in a worker: the task's result
                reporter.log(f"error: {type(exc).__name__}: {exc}")
                results[index] = exc
        reporter.progress(len(tasks), len(tasks), label)
        return results
    pool = (ThreadPoolExecutor(max_workers=workers) if threaded else ProcessPoolExecutor(
        max_workers=min(workers, len(tasks)),
        mp_context=multiprocessing.get_context("spawn")))
    futures: dict[Future, int] = {}
    try:
        for index, (fn, args) in enumerate(tasks):
            futures[pool.submit(_call, fn, args)] = index
        reporter.log(f"  {label or 'tasks'}: {len(tasks)} on {min(workers, len(tasks))} "
                     f"{'thread' if threaded else 'worker'}(s)")
        done_count = 0
        pending = set(futures)
        while pending:
            finished, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            if reporter.cancelled():
                raise Cancelled("cancelled")
            for future in finished:
                result, lines = future.result()
                for line in lines:
                    reporter.log(line)
                results[futures[future]] = result
                done_count += 1
                reporter.progress(done_count, len(tasks), label)
    except BaseException:
        if not threaded:
            for process in list(getattr(pool, "_processes", {}).values()):
                process.terminate()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return results


__all__ = ["count", "default_workers", "processes", "threads"]
