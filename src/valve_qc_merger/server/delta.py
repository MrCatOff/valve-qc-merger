"""pev_body against the server's ``delta.lst``.

The server sends every client the delta descriptions it reads from
``<mod>/delta.lst`` (ReHLDS ``SV_WriteDeltaDescriptionsToClient``), so only
the server's file counts. ``body`` goes out with the bits it gives:
``entity_state_player_t`` for players — and so their p_ model, drawn with the
player's body — and ``entity_state_t`` for every other entity (w_, props).
Valve's file gives 8 bits (0..255), ReGameDLL's 9 for players and 18 for
entities; the field holds up to 32 (``DEFINE_DELTA( body, DT_INTEGER, 32,
1.0 )``). A view model's body is a byte in ``SVC_WEAPONANIM`` whatever the
file says. Merges assume 32 bits (``limits``); :func:`body_report` says what
the server allows and whether the built models fit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from valve_qc_merger.project.model import Project

VALVE_BITS = 8  # Valve's delta.lst (no file: the engine's default description)
PLAYER_KINDS = frozenset({"merge-players", "merge-p"})
ENTITY_KINDS = frozenset({"merge-w", "merge-props"})
_STRUCT = re.compile(r"^\s*(\w+)\s+\w+\s+\w+\s*$")
_BODY = re.compile(r"DEFINE_DELTA\s*\(\s*body\s*,\s*[^,]+,\s*(\d+)\s*,", re.IGNORECASE)


def body_bits(path: Path) -> dict[str, int]:
    """``{structure: bits}`` of the ``body`` field in a delta.lst."""
    bits: dict[str, int] = {}
    current = ""
    for line in Path(path).read_text(encoding="latin-1", errors="replace").splitlines():
        stripped = line.split("//", 1)[0]
        header = _STRUCT.match(stripped)
        if header and "DEFINE_DELTA" not in stripped:
            current = header.group(1)
            continue
        match = _BODY.search(stripped)
        if match and current:
            bits[current] = int(match.group(1))
    return bits


@dataclass
class BodyReport:
    delta: Path | None  # the server's delta.lst (None: not found)
    player_bits: int  # what it gives players (Valve's 8 without a file)
    entity_bits: int
    needed: dict[str, int] = field(default_factory=dict)  # "players"/"entities" -> max body
    builds: dict[str, str] = field(default_factory=dict)  # build -> "players"/"entities"

    def fits(self, kind: str) -> bool:
        bits = self.player_bits if kind == "players" else self.entity_bits
        return self.needed.get(kind, 0) < 2 ** bits

    @property
    def ok(self) -> bool:
        return all(self.fits(kind) for kind in self.needed)

    def text(self) -> str:
        """One paragraph for the Server window."""
        if self.delta is None:
            where = "No delta.lst in the game folder: the engine's (8 bits, 0–255)."
        else:
            where = (f"This server's {self.delta.name}: players {self.player_bits} bits "
                     f"(0–{2 ** self.player_bits - 1}), entities {self.entity_bits} bits "
                     f"(0–{2 ** self.entity_bits - 1}).")
        lines = ["pev_body: view models 0–255 on any server (one byte). Merges assume 32 "
                 "bits for players (and their p_) and other entities (w_, props); the "
                 "server's delta.lst decides — " + where]
        for kind, title in (("players", "players and p_"), ("entities", "w_ and props")):
            if kind in self.needed:
                need = self.needed[kind]
                state = "fits" if self.fits(kind) else (
                    f"needs {max(need.bit_length(), 1)} bits: raise DEFINE_DELTA( body, "
                    "DT_INTEGER, 32, 1.0 ) in "
                    + ("entity_state_player_t" if kind == "players" else "entity_state_t")
                    + " of cstrike/delta.lst")
                lines.append(f"The built {title} use pev_body up to {need} — {state}.")
        return " ".join(lines)


def body_report(project: Project) -> BodyReport:
    """What the server's delta.lst gives ``body`` and what the builds need."""
    from valve_qc_merger.server.package import _manifest
    delta: Path | None = None
    if project.settings.game_dir:
        candidate = Path(project.settings.game_dir) / "delta.lst"
        delta = candidate if candidate.is_file() else None
    bits = body_bits(delta) if delta is not None else {}
    report = BodyReport(delta, bits.get("entity_state_player_t", VALVE_BITS),
                        bits.get("entity_state_t", VALVE_BITS))
    for name, build in sorted(project.builds.items()):
        kind = ("players" if build.kind in PLAYER_KINDS
                else "entities" if build.kind in ENTITY_KINDS else None)
        if kind is None:
            continue
        output = project.build_dir(name) / "output"
        for path in sorted(output.glob("models.*")):
            try:
                entries = _manifest(path)
            except (OSError, ValueError):
                continue
            for entry in entries.values():
                try:
                    body = int(entry.get("pev_body", 0))
                except (TypeError, ValueError):
                    continue
                report.needed[kind] = max(report.needed.get(kind, 0), body)
                report.builds[name] = kind
    return report


__all__ = ["BodyReport", "body_bits", "body_report"]
