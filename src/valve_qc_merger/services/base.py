"""Shared plumbing for services: reporting, cancellation, results, options.

A service is ``run_x(options, reporter) -> ServiceResult``. The CLI passes a
:class:`Reporter` that prints (so command output is unchanged); the GUI passes
a :class:`CallbackReporter` that forwards log lines and progress to the window
and can cancel a running job between models.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

EXIT_OK = 0
EXIT_FAIL = 2       # the job ran but a merge, gate or compile failed
EXIT_DISCOVERY = 3  # bad or missing inputs


class Cancelled(RuntimeError):
    """Raised inside a service when its reporter asks it to stop."""


class Reporter:
    """Default sink: print log lines (CLI behaviour), ignore progress."""

    def log(self, message: str) -> None:
        print(message, flush=True)

    def progress(self, done: int, total: int, label: str = "") -> None:
        """``done`` of ``total`` work units finished; ``label`` names the unit."""

    def cancelled(self) -> bool:
        return False

    def check(self) -> None:
        """Raise :class:`Cancelled` if the job should stop (call between units)."""
        if self.cancelled():
            raise Cancelled("cancelled")


class CallbackReporter(Reporter):
    """Forward log/progress to callbacks; cancel through a threading event."""

    def __init__(
        self,
        on_log: Callable[[str], None] | None = None,
        on_progress: Callable[[int, int, str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self._on_log = on_log
        self._on_progress = on_progress
        self.cancel_event = cancel or threading.Event()

    def log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)

    def progress(self, done: int, total: int, label: str = "") -> None:
        if self._on_progress is not None:
            self._on_progress(done, total, label)

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


class CollectingReporter(Reporter):
    """Keep every log line in memory (tests, batch jobs)."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.events: list[tuple[int, int, str]] = []

    def log(self, message: str) -> None:
        self.lines.append(message)

    def progress(self, done: int, total: int, label: str = "") -> None:
        self.events.append((done, total, label))


@dataclass
class GateRow:
    """One verification-gate check of one output part."""

    part: str
    check: str
    passed: bool
    detail: str


@dataclass
class ServiceResult:
    """What a service produced; ``exit_code`` is the CLI's return value."""

    exit_code: int = EXIT_OK
    outputs: list[Path] = field(default_factory=list)  # emitted QC files
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    gates: list[GateRow] = field(default_factory=list)
    manifest: dict[str, dict[str, Any]] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)  # service-specific extras

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK


OptionsT = TypeVar("OptionsT")


def options_from(cls: type[OptionsT], source: Any) -> OptionsT:
    """Build an options dataclass from any object carrying the same attribute
    names (an argparse namespace); attributes it lacks keep their defaults."""
    values = {
        f.name: getattr(source, f.name)
        for f in dataclasses.fields(cls)  # type: ignore[arg-type]
        if hasattr(source, f.name)
    }
    return cls(**values)


def options_to_dict(options: Any) -> dict[str, Any]:
    """Options as plain TOML-friendly values (Paths become strings, Nones drop)."""
    out: dict[str, Any] = {}
    for f in dataclasses.fields(options):
        value = getattr(options, f.name)
        if value is None:
            continue
        if isinstance(value, Path):
            value = str(value)
        elif isinstance(value, (list, tuple)):
            value = [str(v) if isinstance(v, Path) else v for v in value]
        out[f.name] = value
    return out


def options_from_dict(cls: type[OptionsT], values: dict[str, Any]) -> OptionsT:
    """Inverse of :func:`options_to_dict`: re-type Path fields; reject unknown keys."""
    fields = {f.name: f for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    unknown = sorted(set(values) - set(fields))
    if unknown:
        raise ValueError(f"unknown {cls.__name__} option(s): {', '.join(unknown)}")
    typed: dict[str, Any] = {}
    for name, value in values.items():
        annotation = str(fields[name].type)
        if value is not None and "Path" in annotation and "list" not in annotation:
            value = Path(value)
        typed[name] = value
    return cls(**typed)


__all__ = [
    "CallbackReporter",
    "Cancelled",
    "CollectingReporter",
    "EXIT_DISCOVERY",
    "EXIT_FAIL",
    "EXIT_OK",
    "GateRow",
    "Reporter",
    "ServiceResult",
    "options_from",
    "options_from_dict",
    "options_to_dict",
]
