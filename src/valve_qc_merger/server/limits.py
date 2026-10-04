"""Precache and path limits of the target stack: ReHLDS + ReGameDLL + ReAPI.

Models and sounds stay at 512 — the client's tables, whatever the server
(also after Valve's HL25 update); ReHLDS raised the generic precache (files
sent only for download, e.g. the client sounds of view-model events) to
4096. A model slot is taken by every ``.mdl`` AND every ``.spr`` AND every
brush model of the current map (the world is one of them).
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_MODELS = 512
MAX_SOUNDS = 512
MAX_GENERIC = 4096  # ReHLDS (stock HLDS: 512)
MAX_EVENTS = 256
MAX_RESOURCES = 1280  # resources a client is told about
MAX_PATH = 63  # precache names: 64 bytes with the terminating NUL
WARN_FRACTION = 0.9  # a budget past 90 % is shown as a warning


@dataclass(frozen=True)
class Limit:
    key: str
    title: str
    limit: int
    what: str


LIMITS = (
    Limit("models", "Models", MAX_MODELS,
          ".mdl + .spr + the map's brush models (world included)"),
    Limit("sounds", "Sounds", MAX_SOUNDS, "precache_sound + the map's entity sounds"),
    Limit("generic", "Generic", MAX_GENERIC,
          "precache_generic: download-only files such as view-model client sounds"),
)


def level(used: int, limit: int) -> str:
    """``ok`` / ``warning`` (past 90 %) / ``error`` (over the limit)."""
    if used > limit:
        return "error"
    return "warning" if used >= limit * WARN_FRACTION else "ok"


__all__ = ["LIMITS", "Limit", "MAX_EVENTS", "MAX_GENERIC", "MAX_MODELS", "MAX_PATH",
           "MAX_RESOURCES", "MAX_SOUNDS", "WARN_FRACTION", "level"]
