"""Play a WAV with what the system already has — no audio library in the build.

Windows: ``winsound`` (standard library; one sound at a time, a new one cuts
the last). macOS: ``afplay``. Linux: ``paplay`` or ``aplay``. Every call is
asynchronous; :func:`stop` silences whatever still plays. Good enough to
audition a sound or hear a weapon's events with its animation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_processes: list[subprocess.Popen] = []


def _player() -> list[str] | None:
    if sys.platform == "darwin" and shutil.which("afplay"):
        return ["afplay"]
    for name in ("paplay", "aplay"):
        if shutil.which(name):
            return [name] if name == "paplay" else [name, "-q"]
    return None


def available() -> bool:
    return os.name == "nt" or _player() is not None


def play(path: Path) -> bool:
    """Start ``path`` playing; False when this system has no way to."""
    path = Path(path)
    if not path.is_file():
        return False
    if os.name == "nt":
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC
                           | winsound.SND_NODEFAULT)
        return True
    command = _player()
    if command is None:
        return False
    _processes[:] = [p for p in _processes if p.poll() is None]
    try:
        _processes.append(subprocess.Popen([*command, str(path)],
                                           stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL))
    except OSError:
        return False
    return True


def stop() -> None:
    if os.name == "nt":
        import winsound
        winsound.PlaySound(None, 0)
        return
    for process in _processes:
        if process.poll() is None:
            process.terminate()
    _processes.clear()


__all__ = ["available", "play", "stop"]
