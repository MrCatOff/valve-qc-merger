"""Server config files (``game.cfg``, ``server.cfg``): read, document, edit.

ReGameDLL ships ``game.cfg`` with every cvar documented in the comment block
above it::

    // Auto balancing of teams
    // 0 - disabled
    // 1 - on after next round (default behaviour)
    //
    // Default value: "1"
    mp_autoteambalance "1"

:func:`parse` reads that layout into :class:`Cvar` docs. The studio bundles
reference copies (ReGameDLL's ``game.cfg`` and the ReHLDS cvars from its
README, both MIT) under ``storage/server`` so a server whose own file lost its
comments still gets descriptions and defaults. :class:`CfgFile` edits values in
place: comments, order and unknown lines survive a save.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.resources import data_root

_CVAR = re.compile(r'^\s*([A-Za-z_][\w.]*)\s+("([^"]*)"|(\S+))?\s*(//.*)?$')
_OPTION = re.compile(r"^\s*(-?[\w.]+)\s+-\s+(.*)$")
_DEFAULT = re.compile(r'^Default(?: value)?:\s*"([^"]*)"')
_COMMANDS = {"echo", "exec", "alias", "bind", "say", "map", "changelevel", "wait",
             "rcon_password", "sv_password"}
# commands that are lines of a cfg but not cvars to edit (passwords are cvars,
# but never shown in a table someone may screenshot)

REFERENCES = {
    "game": ("ReGameDLL (game.cfg)", "regamedll_game.cfg"),
    "server": ("ReHLDS (server.cfg)", "rehlds_server.cfg"),
}


GROUPS = {"mp": "Gameplay (mp_)", "sv": "Server (sv_)", "bot": "Bots (bot_)",
          "other": "Other"}


@dataclass
class Cvar:
    name: str
    value: str
    default: str | None = None
    description: str = ""
    options: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # flag lists, examples, NOTE: lines

    @property
    def group(self) -> str:
        return group_of(self.name)

    @property
    def changed(self) -> bool:
        return self.default is not None and self.value != self.default


def group_of(name: str) -> str:
    """``mp_buytime`` -> ``mp``; ``sv_rehlds_force_dlmax`` -> ``sv``."""
    head = name.split("_", 1)[0].lower()
    return head if head in GROUPS and "_" in name else "other"


def _doc(comments: list[str], name: str, value: str) -> Cvar:
    cvar = Cvar(name, value)
    text: list[str] = []
    for line in comments:
        if (match := _DEFAULT.match(line)) is not None:
            cvar.default = match.group(1)
        elif (match := _OPTION.match(line)) is not None and not cvar.notes:
            cvar.options.append((match.group(1), match.group(2).strip()))
        elif cvar.options or cvar.notes:
            if line:
                cvar.notes.append(line)
        elif line:
            text.append(line)
    cvar.description = "\n".join(text)
    return cvar


def parse(text: str) -> list[Cvar]:
    """The cvars of a cfg, each with the comment block right above it."""
    out: list[Cvar] = []
    comments: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("//"):
            comments.append(line[2:].strip())
            continue
        if not line:
            comments = []
            continue
        match = _CVAR.match(line)
        if match is None or match.group(1).lower() in _COMMANDS:
            comments = []
            continue
        value = match.group(3) if match.group(3) is not None else (match.group(4) or "")
        out.append(_doc(comments, match.group(1), value))
        comments = []
    return out


def reference(kind: str) -> list[Cvar]:
    """The bundled documented cvars: ``game`` (ReGameDLL) or ``server`` (ReHLDS)."""
    path = data_root() / "storage" / "server" / REFERENCES[kind][1]
    return parse(path.read_text(encoding="utf-8")) if path.is_file() else []


def documented(text: str, kind: str) -> list[Cvar]:
    """The cvars of ``text`` with descriptions/defaults filled in from the
    reference, followed by the reference cvars the file does not set (their
    value is the default)."""
    ref = {c.name.lower(): c for c in reference(kind)}
    seen: set[str] = set()
    out = []
    for cvar in parse(text):
        doc = ref.get(cvar.name.lower())
        if doc is not None:
            cvar.description = cvar.description or doc.description
            cvar.default = cvar.default if cvar.default is not None else doc.default
            cvar.options = cvar.options or doc.options
            cvar.notes = cvar.notes or doc.notes
        seen.add(cvar.name.lower())
        out.append(cvar)
    for key, doc in ref.items():
        if key not in seen:
            out.append(Cvar(doc.name, doc.default or "", doc.default, doc.description,
                            list(doc.options), list(doc.notes)))
    return out


def _quote(value: str) -> str:
    return f'"{value}"'


class CfgFile:
    """A cfg's lines with in-place value edits (the rest is kept verbatim)."""

    def __init__(self, text: str = "") -> None:
        self.lines = text.splitlines()

    @classmethod
    def load(cls, path: Path) -> CfgFile:
        path = Path(path)
        return cls(path.read_text(encoding="utf-8", errors="replace") if path.is_file()
                   else "")

    def _find(self, name: str) -> int | None:
        found = None
        for index, line in enumerate(self.lines):
            match = _CVAR.match(line.strip())
            if match and match.group(1).lower() == name.lower():
                found = index  # the last line wins, as when the engine execs it
        return found

    def get(self, name: str) -> str | None:
        index = self._find(name)
        if index is None:
            return None
        match = _CVAR.match(self.lines[index].strip())
        assert match is not None
        return match.group(3) if match.group(3) is not None else (match.group(4) or "")

    def set(self, name: str, value: str, *, doc: Cvar | None = None) -> None:
        """Change ``name`` where the file sets it; append it otherwise (with
        its description from ``doc``)."""
        index = self._find(name)
        if index is not None:
            line = self.lines[index]
            indent = line[:len(line) - len(line.lstrip())]
            match = _CVAR.match(line.strip())
            assert match is not None
            tail = f" {match.group(5)}" if match.group(5) else ""
            self.lines[index] = f"{indent}{match.group(1)} {_quote(value)}{tail}"
            return
        if self.lines and self.lines[-1].strip():
            self.lines.append("")
        if doc is not None:
            self.lines += [f"// {line}" for line in doc.description.splitlines()]
            self.lines += [f"// {v} - {text}" for v, text in doc.options]
            if doc.default is not None:
                self.lines.append(f'// Default value: "{doc.default}"')
        self.lines.append(f"{name} {_quote(value)}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"

    def save(self, path: Path) -> Path | None:
        """Write the file; the previous one is kept as ``<name>.bak``
        (returned, or ``None`` for a new file)."""
        path = Path(path)
        backup = None
        if path.is_file():
            backup = path.with_name(path.name + ".bak")
            backup.write_bytes(path.read_bytes())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.text(), encoding="utf-8", newline="\n")
        return backup


__all__ = ["CfgFile", "Cvar", "GROUPS", "REFERENCES", "documented", "group_of", "parse",
           "reference"]
