"""Preflight: what studiomdl would fail on, or silently corrupt, in a QC —
found before it runs, with a message that says what to change.

The QC is read as BYTES, the way studiomdl reads it: a label or path of
another code page is compared with the file names on disk byte for byte
(the ``"\\ufffd\\ufffd KakTyc"`` sequence: written into the QC as one byte
string and onto the disk as another, studiomdl stops at "doesn't exist").

Errors stop the compile (studiomdl fails, or writes a broken model):

- a ``$body`` / ``studio`` / ``$sequence`` SMD that is not on disk under that
  exact name (another case only matches on Windows);
- a sequence label longer than 31 bytes (strcpy into ``char[32]``);
- more than 32 submodels in one bodygroup, or 32 bodygroups (fixed arrays:
  memory corruption — meshes detach from their bones);
- a material with a space (the SMD triangle tokenizer splits it) or longer
  than 63 bytes;
- more than 128 bones in the reference meshes.

Warnings: non-ASCII labels, paths and materials (they compile only while the
QC and the files keep the same bytes). Duplicate labels and bodygroup names
(decompilers write them) compile fine and are not reported.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SEQ_LABEL_LIMIT = 31
SUBMODEL_LIMIT = 32
BODYPART_LIMIT = 32
BONE_LIMIT = 128
MATERIAL_LIMIT = 63

# $sequence options and how many values follow each (anything else that is
# not an ACT_ activity or a number is an animation file, as for studiomdl)
_SEQ_OPTIONS = {b"fps": 1, b"origin": 3, b"rotate": 1, b"scale": 1, b"frame": 2,
                b"blend": 3, b"node": 1, b"transition": 2, b"rtransition": 2,
                b"pivot": 3, b"loop": 0, b"animation": 0}
_MOTION = {b"x", b"y", b"z", b"xr", b"yr", b"zr", b"lx", b"ly", b"lz", b"lxr", b"lyr",
           b"lzr", b"ax", b"ay", b"az", b"axr", b"ayr", b"azr"}


@dataclass(frozen=True)
class QcProblem:
    level: str  # "error" | "warning"
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.message}"


def _tokens(data: bytes) -> list[bytes]:
    """studiomdl's tokens: quoted strings (quotes dropped), braces, words;
    ``//`` comments skipped."""
    out: list[bytes] = []
    i, n = 0, len(data)
    while i < n:
        ch = data[i:i + 1]
        if ch.isspace():
            i += 1
        elif data.startswith(b"//", i):
            end = data.find(b"\n", i)
            i = n if end < 0 else end + 1
        elif ch == b'"':
            end = data.find(b'"', i + 1)
            end = n if end < 0 else end
            out.append(data[i + 1:end])
            i = end + 1
        elif ch in (b"{", b"}"):
            out.append(ch)
            i += 1
        else:
            j = i
            while j < n and not data[j:j + 1].isspace() and data[j:j + 1] not in (b"{", b"}",
                                                                                b'"'):
                j += 1
            out.append(data[i:j])
            i = j
    return out


def _is_number(token: bytes) -> bool:
    try:
        float(token)
    except ValueError:
        return False
    return True


def _ascii(raw: bytes) -> bool:
    """Printable ASCII (a space is fine inside quotes)."""
    return all(0x20 <= b <= 0x7E for b in raw)


def _show(raw: bytes) -> str:
    return raw.decode("latin-1")


@dataclass
class _Qc:
    cd: bytes = b"."
    bodies: list[tuple[bytes, bytes]] = None  # (bodygroup name, smd)
    groups: list[tuple[bytes, int]] = None  # (name, submodels)
    sequences: list[tuple[bytes, list[bytes]]] = None  # (label, files)


def _parse(data: bytes) -> _Qc:
    qc = _Qc(bodies=[], groups=[], sequences=[])
    tokens = _tokens(data)
    i = 0

    def block(start: int) -> tuple[list[bytes], int]:
        """Tokens inside the ``{`` at ``start`` (nested braces kept)."""
        depth, j = 0, start
        while j < len(tokens):
            if tokens[j] == b"{":
                depth += 1
            elif tokens[j] == b"}":
                depth -= 1
                if depth == 0:
                    return tokens[start + 1:j], j + 1
            j += 1
        return tokens[start + 1:], len(tokens)

    while i < len(tokens):
        word = tokens[i].lower()
        if word == b"$cd" and i + 1 < len(tokens):
            qc.cd = tokens[i + 1]
            i += 2
        elif word == b"$body" and i + 2 < len(tokens):
            qc.groups.append((tokens[i + 1], 1))
            qc.bodies.append((tokens[i + 1], tokens[i + 2]))
            i += 3
        elif word == b"$bodygroup" and i + 2 < len(tokens) and tokens[i + 2] == b"{":
            name = tokens[i + 1]
            inner, i = block(i + 2)
            count, k = 0, 0
            while k < len(inner):
                if inner[k].lower() == b"studio" and k + 1 < len(inner):
                    qc.bodies.append((name, inner[k + 1]))
                    count += 1
                    k += 2
                else:
                    count += inner[k].lower() == b"blank"
                    k += 1
            qc.groups.append((name, count))
        elif word == b"$sequence" and i + 1 < len(tokens):
            label = tokens[i + 1]
            if i + 2 < len(tokens) and tokens[i + 2] == b"{":
                body, i = block(i + 2)
            else:
                j = i + 2
                while j < len(tokens) and not tokens[j].startswith(b"$"):
                    j += 1
                body, i = tokens[i + 2:j], j
            qc.sequences.append((label, _sequence_files(body)))
        else:
            i += 1
    return qc


def _sequence_files(body: list[bytes]) -> list[bytes]:
    files: list[bytes] = []
    k, depth = 0, 0
    while k < len(body):
        token, low = body[k], body[k].lower()
        if token == b"{":
            depth += 1
        elif token == b"}":
            depth -= 1
        elif depth > 0:  # an event block
            pass
        elif low == b"event":
            k += 1
            if k < len(body) and body[k] != b"{":
                k += 2  # old form: event <id> <frame> ["options"]
                if k + 1 < len(body) and not _is_number(body[k + 1]) \
                        and not body[k + 1].startswith(b"$"):
                    k += 1
            continue
        elif low in _SEQ_OPTIONS:
            k += _SEQ_OPTIONS[low]
        elif low.startswith(b"act_"):
            if k + 1 < len(body) and _is_number(body[k + 1]):
                k += 1
        elif low in _MOTION or _is_number(token):
            pass
        else:
            files.append(token)
        k += 1
    return files


def _smd(folder: Path, cd: bytes, name: bytes) -> Path:
    raw = name.replace(b"\\", b"/")
    if not raw.lower().endswith(b".smd"):
        raw += b".smd"
    base = os.fsencode(folder) + b"/" + cd.replace(b"\\", b"/")
    return Path(os.fsdecode(base + b"/" + raw))


def _case_match(path: Path) -> Path | None:
    """The file ``path`` names in another case, if one exists."""
    parent = path.parent
    if not parent.is_dir():
        return None
    lowered = path.name.lower()
    return next((p for p in parent.iterdir() if p.name.lower() == lowered), None)


def _smd_facts(path: Path) -> tuple[set[bytes], set[bytes]]:
    """(bone names, materials) of an SMD."""
    bones: set[bytes] = set()
    materials: set[bytes] = set()
    section = b""
    line_no = 0
    for raw in path.read_bytes().splitlines():
        line = raw.strip()
        if section == b"":
            if line in (b"nodes", b"skeleton", b"triangles"):
                section, line_no = line, 0
            continue
        if line == b"end":
            section = b""
            continue
        if section == b"nodes":
            first = line.find(b'"')
            last = line.rfind(b'"')
            if 0 <= first < last:
                bones.add(line[first + 1:last])
        elif section == b"triangles":
            if line_no % 4 == 0 and line:
                materials.add(line)
            line_no += 1
    return bones, materials


def check_qc(qc_path: Path) -> list[QcProblem]:
    """Everything studiomdl would trip over in ``qc_path`` (see the module)."""
    qc_path = Path(qc_path)
    qc = _parse(qc_path.read_bytes())
    folder = qc_path.parent
    problems: list[QcProblem] = []

    def error(message: str) -> None:
        problems.append(QcProblem("error", message))

    def warn(message: str) -> None:
        problems.append(QcProblem("warning", message))

    def file_exists(raw: bytes, what: str) -> Path | None:
        path = _smd(folder, qc.cd, raw)
        if path.is_file():
            if not _ascii(raw):
                warn(f"{what} file {_show(raw)!r} is not plain ASCII: it compiles only while "
                     "the QC and the file keep the same bytes")
            return path
        other = _case_match(path)
        if other is not None:
            error(f"{what} file {_show(raw)!r} is on disk as {other.name!r}: only Windows "
                  "ignores the case — rename one to match")
            return other
        hint = ("; its name has non-ASCII bytes — the QC and the disk spell it differently, "
                "rename both to plain ASCII" if not _ascii(raw) else "")
        error(f"{what} file {_show(raw)!r} is missing ({path.name}){hint}")
        return None

    if len(qc.groups) > BODYPART_LIMIT:
        error(f"{len(qc.groups)} bodygroups > {BODYPART_LIMIT} (studiomdl overruns its "
              "array: a broken model)")
    for name, count in qc.groups:
        if count > SUBMODEL_LIMIT:
            error(f"bodygroup {_show(name)!r} has {count} submodels > {SUBMODEL_LIMIT} "
                  "(meshes detach from their bones in game)")

    bones: set[bytes] = set()
    materials: set[bytes] = set()
    for group, raw in qc.bodies:
        path = file_exists(raw, f"bodygroup {_show(group)!r} mesh")
        if path is not None:
            found_bones, found_materials = _smd_facts(path)
            bones |= found_bones
            materials |= found_materials
    if len(bones) > BONE_LIMIT:
        error(f"{len(bones)} bones in the meshes > {BONE_LIMIT}")
    for material in sorted(materials):
        if b" " in material or b"\t" in material:
            error(f"material {_show(material)!r} has a space: studiomdl splits it and "
                  "crashes — rename the texture")
        elif len(material) > MATERIAL_LIMIT:
            error(f"material {_show(material)!r} is longer than {MATERIAL_LIMIT} bytes")
        elif not _ascii(material):
            warn(f"material {_show(material)!r} is not plain ASCII")

    for label, files in qc.sequences:
        shown = _show(label)
        if len(label) > SEQ_LABEL_LIMIT:
            error(f"sequence label {shown!r} is {len(label)} bytes > {SEQ_LABEL_LIMIT}")
        if not _ascii(label):
            warn(f"sequence label {shown!r} is not plain ASCII")
        if not files:
            error(f"sequence {shown!r} names no animation file")
        for raw in files:
            file_exists(raw, f"sequence {shown!r} animation")
    return problems


__all__ = ["QcProblem", "check_qc"]
