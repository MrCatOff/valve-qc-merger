"""Data-file resolution that works both from a checkout and a frozen exe.

PyInstaller one-file builds unpack bundled data into a temp directory exposed
as ``sys._MEIPASS``; repo-relative defaults like the reference hands SMD must
fall back to that location when the file is not present next to the user's
working directory.
"""

from __future__ import annotations

import sys
from pathlib import Path


def data_root() -> Path:
    """Where bundled data (``storage/...``) lives: the PyInstaller unpack
    directory when frozen, else the checkout holding ``pyproject.toml``,
    else the working directory."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle is not None:
        return Path(bundle)
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.cwd()


def resource_path(relative: str | Path) -> Path:
    """Resolve a repo-relative data file from cwd first, then the exe bundle."""
    candidate = Path(relative)
    if candidate.exists():
        return candidate
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle is not None:
        frozen = Path(bundle) / relative
        if frozen.exists():
            return frozen
    return candidate


__all__ = ["data_root", "resource_path"]
