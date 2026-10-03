"""QC edits on an asset (Qt-free): sequence properties and events, texture
render modes, and the raw text.

Like the bone tools (:mod:`.bones`) every edit rewrites files in the asset
folder and returns a result the studio logs; the studio snapshots the asset
first, so Undo restores it. A sequence block is rebuilt in the braced form:
its animation paths and any option this module does not manage (``blend``,
``origin``, ``LX`` …) are kept verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.retarget.qc_build import _matching_brace

RENDER_MODES = ("masked", "additive", "fullbright", "flatshade", "chrome")
LABEL_LIMIT = 31  # studiomdl keeps 32-byte labels

# Client-side animation events the CS 1.6 client handles (HL SDK cl_dll).
EVENT_NAMES = {
    5001: "muzzle flash, attachment 0",
    5011: "muzzle flash, attachment 1",
    5021: "muzzle flash, attachment 2",
    5031: "muzzle flash, attachment 3",
    5002: "sparks",
    5004: "client sound (option = sound path)",
}

_SEQ_RE = re.compile(r"\$sequence\b", re.IGNORECASE)
_TOKEN_RE = re.compile(r'\s*(?:"(?P<q>[^"]+)"|(?P<w>[^\s{}"]+))')
_EVENT_BLOCK_RE = re.compile(r"\{\s*event\b[^}]*\}", re.IGNORECASE)
_EVENT_RE = re.compile(
    r'\{\s*event\s+(?P<id>-?\d+)\s+(?P<frame>-?\d+)(?:\s+"(?P<opt>[^"]*)")?\s*\}',
    re.IGNORECASE)
_RENDER_RE = re.compile(r'^[ \t]*\$texrendermode\s+"(?P<tex>[^"]+)"\s+\w+[ \t]*\n?',
                        re.IGNORECASE | re.MULTILINE)


class QcEditError(ValueError):
    """An edit that cannot be applied (bad value, unknown sequence)."""


@dataclass
class QcEditResult:
    files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    max_pose_deviation: float = 0.0  # QC edits never move bones


@dataclass
class SequenceEvent:
    event: int
    frame: int
    option: str = ""

    def qc(self) -> str:
        option = f' "{self.option}"' if self.option else ""
        return f"{{ event {self.event} {self.frame}{option} }}"


@dataclass
class SequenceBlock:
    """One ``$sequence`` as the editor sees it."""

    name: str
    paths: list[str]
    fps: float | None
    loop: bool
    activity: str | None  # "ACT_IDLE 1"
    events: list[SequenceEvent]
    other: str  # unmanaged options, verbatim ("blend XR -45 45 LX")
    span: tuple[int, int]  # in the QC text, keyword to closing brace/line end


def qc_file(directory: Path) -> Path:
    """The QC the rest of the program reads (the first, sorted)."""
    found = sorted(Path(directory).glob("*.qc"))
    if not found:
        raise QcEditError(f"no .qc in {directory}")
    return found[0]


def _read(directory: Path) -> tuple[Path, str]:
    path = qc_file(directory)
    return path, path.read_text(encoding="latin-1")


def parse_blocks(text: str) -> list[SequenceBlock]:
    """Every ``$sequence`` block, in file order (braced or one-line form)."""
    blocks: list[SequenceBlock] = []
    for keyword in _SEQ_RE.finditer(text):
        name_m = _TOKEN_RE.match(text, keyword.end())
        if name_m is None:
            continue
        name = name_m.group("q") or name_m.group("w")
        cursor = name_m.end()
        while cursor < len(text) and text[cursor] in " \t":
            cursor += 1
        if cursor < len(text) and text[cursor] == "{":
            close = _matching_brace(text, cursor)
            body, end = text[cursor + 1:close], close + 1
        else:
            line_end = text.find("\n", cursor)
            line_end = len(text) if line_end == -1 else line_end
            body, end = text[cursor:line_end], line_end
        events = [SequenceEvent(int(m.group("id")), int(m.group("frame")), m.group("opt") or "")
                  for m in _EVENT_RE.finditer(body)]
        plain = _EVENT_BLOCK_RE.sub(" ", body)
        paths = re.findall(r'"([^"]+)"', plain)
        rest = re.sub(r'"[^"]*"', " ", plain)
        fps_m = re.search(r"\bfps\s+([0-9.]+)", rest, re.IGNORECASE)
        rest = re.sub(r"\bfps\s+[0-9.]+", " ", rest, flags=re.IGNORECASE)
        loop = re.search(r"\bloop\b", rest, re.IGNORECASE) is not None
        rest = re.sub(r"\bloop\b", " ", rest, flags=re.IGNORECASE)
        act_m = re.search(r"\b(ACT_[A-Za-z0-9_]+)(?:\s+([0-9.]+))?", rest)
        rest = re.sub(r"\bACT_[A-Za-z0-9_]+(?:\s+[0-9.]+)?", " ", rest)
        blocks.append(SequenceBlock(
            name=name, paths=paths, fps=float(fps_m.group(1)) if fps_m else None,
            loop=loop,
            activity=" ".join(g for g in act_m.groups() if g) if act_m else None,
            events=events, other=" ".join(rest.split()),
            span=(keyword.start(), end)))
    return blocks


def _format_fps(fps: float) -> str:
    return str(int(fps)) if fps == int(fps) else f"{fps:g}"


def format_block(block: SequenceBlock) -> str:
    lines = [f'$sequence "{block.name}" {{']
    lines += [f'\t"{path}"' for path in block.paths]
    if block.other:
        lines.append(f"\t{block.other}")
    lines += [f"\t{event.qc()}" for event in block.events]
    if block.fps is not None:
        lines.append(f"\tfps {_format_fps(block.fps)}")
    if block.loop:
        lines.append("\tloop")
    if block.activity:
        lines.append(f"\t{block.activity}")
    lines.append("}")
    return "\n".join(lines)


_KEEP = object()


def set_sequence(directory: Path, index: int, *, name: str | None = None,
                 fps: float | None | object = _KEEP, loop: bool | None = None,
                 activity: str | None | object = _KEEP,
                 events: list[SequenceEvent] | None = None) -> QcEditResult:
    """Change one sequence (by position). Unspecified fields keep their value;
    ``fps=None`` / ``activity=None`` remove the line."""
    path, text = _read(directory)
    blocks = parse_blocks(text)
    if not 0 <= index < len(blocks):
        raise QcEditError(f"no sequence #{index}")
    block = blocks[index]
    if name is not None:
        name = name.strip()
        if not name or '"' in name or any(c.isspace() for c in name):
            raise QcEditError("a sequence name needs letters, digits or _ (no spaces)")
        if len(name) > LABEL_LIMIT:
            raise QcEditError(f"a sequence name keeps {LABEL_LIMIT} characters")
        if any(b.name.lower() == name.lower() for i, b in enumerate(blocks) if i != index):
            raise QcEditError(f"another sequence is already called {name!r}")
        block.name = name
    if fps is not _KEEP:
        if fps is not None and not 0 < float(fps) <= 1000:  # type: ignore[arg-type]
            raise QcEditError("fps must be between 0 and 1000")
        block.fps = float(fps) if fps is not None else None  # type: ignore[arg-type]
    if loop is not None:
        block.loop = loop
    if activity is not _KEEP:
        activity = (activity or "").strip() or None  # type: ignore[union-attr]
        if activity is not None and not re.fullmatch(r"ACT_[A-Za-z0-9_]+(\s+[0-9.]+)?",
                                                     activity):
            raise QcEditError("an activity looks like ACT_IDLE or ACT_IDLE 1")
        block.activity = activity  # type: ignore[assignment]
    if events is not None:
        for event in events:
            if '"' in event.option or "}" in event.option:
                raise QcEditError("event options cannot contain quotes or braces")
        block.events = sorted(events, key=lambda e: (e.frame, e.event))
    start, end = block.span
    path.write_text(text[:start] + format_block(block) + text[end:], encoding="latin-1")
    return QcEditResult(files=[path])


def set_render_mode(directory: Path, texture: str, mode: str | None) -> QcEditResult:
    """Set (or with ``None`` remove) a texture's ``$texrendermode``."""
    if mode is not None and mode not in RENDER_MODES:
        raise QcEditError(f"render mode must be one of {', '.join(RENDER_MODES)}")
    path, text = _read(directory)
    matches = [m for m in _RENDER_RE.finditer(text)
               if m.group("tex").lower() == texture.lower()]
    for match in reversed(matches):
        text = text[:match.start()] + text[match.end():]
    if mode is not None:
        line = f'$texrendermode "{texture}" {mode}\n'
        anchor = matches[0].start() if matches else None
        if anchor is None:
            first = re.search(r"^[ \t]*\$(?:bodygroup|body)\b", text,
                              re.IGNORECASE | re.MULTILINE)
            anchor = first.start() if first else len(text)
        text = text[:anchor] + line + text[anchor:]
    path.write_text(text, encoding="latin-1")
    return QcEditResult(files=[path])


def write_qc(directory: Path, text: str) -> QcEditResult:
    """Replace the QC text, then prove the model still loads; a QC that does
    not is put back and the error raised."""
    from valve_qc_merger.merge_view.discovery import MergeViewError, load_model
    path, before = _read(directory)

    def missing() -> set[str]:
        model = load_model(Path(directory), require_anims=False)
        return {w for w in model.warnings if "missing" in w or "no SMD path" in w}

    try:
        known = missing()
    except (MergeViewError, ValueError, OSError):
        known = set()
    path.write_text(text, encoding="latin-1")
    try:
        new = missing() - known
        if new:
            raise QcEditError("; ".join(sorted(new)))
    except (MergeViewError, ValueError, OSError) as exc:
        path.write_text(before, encoding="latin-1")
        raise QcEditError(f"the QC no longer loads, not saved: {exc}") from exc
    return QcEditResult(files=[path])


__all__ = ["EVENT_NAMES", "QcEditError", "QcEditResult", "RENDER_MODES", "SequenceBlock",
           "SequenceEvent", "format_block", "parse_blocks", "qc_file", "set_render_mode",
           "set_sequence", "write_qc"]
