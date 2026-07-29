"""Lightweight, text-level editing of QC files.

The hand-replacement command only needs to *read* a weapon's bodygroups and
*rewrite* the hands bodygroup in place, leaving the rest of the QC (sequences,
events, hitboxes, comments, formatting) byte-for-byte untouched. A surgical
text editor is safer for that than a full parse-and-regenerate round trip, so
this module works directly on the QC source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BODYGROUP_RE = re.compile(r'\$bodygroup\s+"?(?P<name>[^"\s{]+)"?\s*', re.IGNORECASE)
_STUDIO_RE = re.compile(r'studio\s+"?(?P<path>[^"\s]+)"?', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class BodyGroupBlock:
    """A ``$bodygroup`` located in the QC text, with its character span."""

    name: str
    studios: tuple[str, ...]
    start: int  # index of the '$' in "$bodygroup"
    end: int  # index just past the closing '}'


def _match_brace(text: str, open_index: int) -> int:
    """Return the index just past the '}' matching the '{' at ``open_index``."""
    depth = 0
    for index in range(open_index, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    raise ValueError("unbalanced braces in $bodygroup block")


def find_bodygroups(text: str) -> list[BodyGroupBlock]:
    """Return every ``$bodygroup`` block found in ``text``."""
    blocks: list[BodyGroupBlock] = []
    for match in _BODYGROUP_RE.finditer(text):
        brace = text.find("{", match.end())
        if brace == -1:
            continue
        end = _match_brace(text, brace)
        body = text[brace + 1 : end - 1]
        studios = tuple(m.group("path") for m in _STUDIO_RE.finditer(body))
        blocks.append(BodyGroupBlock(match.group("name"), studios, match.start(), end))
    return blocks


def find_bodygroup(text: str, name: str) -> BodyGroupBlock | None:
    """Return the first bodygroup whose name matches ``name`` (case-insensitive)."""
    for block in find_bodygroups(text):
        if block.name.lower() == name.lower():
            return block
    return None


def replace_bodygroup_studios(text: str, block: BodyGroupBlock, studios: list[str]) -> str:
    """Return ``text`` with ``block`` rewritten to use ``studios``."""
    body = "\n".join(f'\tstudio "{studio}"' for studio in studios)
    replacement = f'$bodygroup "{block.name}"\n{{\n{body}\n}}'
    return text[: block.start] + replacement + text[block.end :]


__all__ = [
    "BodyGroupBlock",
    "find_bodygroup",
    "find_bodygroups",
    "replace_bodygroup_studios",
]
