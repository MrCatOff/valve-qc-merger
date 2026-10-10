"""Portable names for an imported model folder: every name a QC gives —
sequence labels, bodygroup names, the SMD files it reads, the textures its
meshes use — becomes ``[A-Za-z0-9_]`` (letters, digits, ``_``), the same on
Windows, macOS and Linux, and the files on disk are renamed to match.

Decompiled models carry names of other code pages (CSO's CP949 / CP1251
``"äëÿ KakTycà"``), spaces, brackets and dashes; written into a QC as one byte
string and onto the disk as another, studiomdl cannot find the file, and a
name that only differs in case finds it on Windows alone. Bone names are kept
(``Bip01 R Hand`` is what the rigs are matched by) and so is the model's own
name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SEQ_LABEL_LIMIT = 31  # studiomdl: char label[32]
NAME_LIMIT = 60  # file stems / bodygroup names (studiomdl: 64-byte buffers)
_INVALID = re.compile(r"[^A-Za-z0-9_]+")


def portable(name: str, fallback: str = "x", limit: int = NAME_LIMIT) -> str:
    """``name`` as ``[A-Za-z0-9_]``: every run of other characters is one
    ``_`` (``"äëÿ KakTycà"`` -> ``"_KakTyc_"``)."""
    return (_INVALID.sub("_", name)[:limit]) or fallback


class _Unique:
    """Hands out portable names, unique ignoring case."""

    def __init__(self, limit: int = NAME_LIMIT) -> None:
        self.taken: set[str] = set()
        self.limit = limit

    def __call__(self, name: str, fallback: str = "x") -> str:
        base = portable(name, fallback, self.limit)
        candidate, counter = base, 2
        while candidate.lower() in self.taken:
            suffix = f"_{counter}"
            candidate = base[:self.limit - len(suffix)] + suffix
            counter += 1
        self.taken.add(candidate.lower())
        return candidate


@dataclass
class _Token:
    start: int
    end: int
    text: str
    quoted: bool


def _tokens(text: str) -> list[_Token]:
    out: list[_Token] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif ch == '"':
            end = text.find('"', i + 1)
            end = n if end < 0 else end
            out.append(_Token(i, end + 1, text[i + 1:end], True))
            i = end + 1
        elif ch in "{}":
            out.append(_Token(i, i + 1, ch, False))
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '{}"':
                j += 1
            out.append(_Token(i, j, text[i:j], False))
            i = j
    return out


_SEQ_OPTIONS = {"fps": 1, "origin": 3, "rotate": 1, "scale": 1, "frame": 2, "blend": 3,
                "node": 1, "transition": 2, "rtransition": 2, "pivot": 3, "loop": 0,
                "animation": 0}
_MOTION = {"x", "y", "z", "xr", "yr", "zr", "lx", "ly", "lz", "lxr", "lyr", "lzr", "ax", "ay",
           "az", "axr", "ayr", "azr"}


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


@dataclass
class NormalizeReport:
    renamed: dict[str, str] = field(default_factory=dict)  # "kind: old" -> new

    def note(self, kind: str, old: str, new: str) -> None:
        if old != new:
            self.renamed[f"{kind}: {old}"] = new


def _find_file(folder: Path, relative: str) -> Path | None:
    """``folder/relative`` on disk, matching each part ignoring case."""
    current = folder
    for part in [p for p in relative.replace("\\", "/").split("/") if p and p != "."]:
        if part == "..":
            current = current.parent
            continue
        if not current.is_dir():
            return None
        # the name as the disk spells it (a case-insensitive disk also
        # "finds" other spellings)
        entries = list(current.iterdir())
        match = next((p for p in entries if p.name == part), None) or next(
            (p for p in entries if p.name.lower() == part.lower()), None)
        if match is None:
            return None
        current = match
    return current if current.is_file() else None


def normalize_model(folder: Path) -> NormalizeReport:
    """Rename what the QC of ``folder`` names (see the module) in place."""
    folder = Path(folder)
    qcs = sorted(folder.glob("*.qc"))
    report = NormalizeReport()
    if not qcs:
        return report
    qc_path = qcs[0]
    text = qc_path.read_text(encoding="latin-1")
    tokens = _tokens(text)
    edits: dict[int, tuple[_Token, str]] = {}  # token start -> (token, new text)

    def replace(token: _Token, new: str) -> None:
        if new != token.text:
            edits[token.start] = (token, new)

    cd, cdtextures = ".", []
    for k, token in enumerate(tokens[:-1]):
        word = token.text.lower()
        if word == "$cd":
            cd = tokens[k + 1].text
        elif word == "$cdtexture":
            cdtextures.append(tokens[k + 1].text)
    base = folder / cd
    texture_dirs = [folder / d for d in cdtextures] + [base, folder]

    labels = _Unique(SEQ_LABEL_LIMIT)
    groups = _Unique()
    file_names: dict[Path, str] = {}  # resolved SMD -> new relative stem
    per_dir: dict[Path, _Unique] = {}
    smds: list[Path] = []

    def smd_ref(token: _Token) -> None:
        """An SMD reference: give the file a portable name, rename it."""
        raw = token.text.replace("\\", "/")
        with_ext = raw if raw.lower().endswith(".smd") else raw + ".smd"
        path = _find_file(base, with_ext)
        if path is None:
            return
        if path not in file_names:
            parts = [p for p in raw.split("/")]
            dirs = [p if p in (".", "..", "") else portable(p) for p in parts[:-1]]
            unique = per_dir.setdefault(path.parent, _Unique())
            stem = unique(Path(parts[-1]).stem if raw.lower().endswith(".smd")
                          else parts[-1], "mesh")
            new_rel = "/".join([*dirs, stem]) if dirs else stem
            file_names[path] = new_rel
            smds.append(path)
        new = file_names[path]
        replace(token, new + (".smd" if raw.lower().endswith(".smd") else ""))

    i = 0
    while i < len(tokens):
        word = tokens[i].text.lower()
        if word == "$body" and i + 2 < len(tokens):
            name = groups(tokens[i + 1].text, "body")
            report.note("bodygroup", tokens[i + 1].text, name)
            replace(tokens[i + 1], name)
            smd_ref(tokens[i + 2])
            i += 3
        elif word == "$bodygroup" and i + 2 < len(tokens):
            name = groups(tokens[i + 1].text, "body")
            report.note("bodygroup", tokens[i + 1].text, name)
            replace(tokens[i + 1], name)
            i += 2
            if tokens[i].text == "{":
                i += 1
                while i < len(tokens) and tokens[i].text != "}":
                    if tokens[i].text.lower() == "studio" and i + 1 < len(tokens):
                        smd_ref(tokens[i + 1])
                        i += 2
                    else:
                        i += 1
            i += 1
        elif word == "$sequence" and i + 1 < len(tokens):
            label = labels(tokens[i + 1].text, "seq")
            report.note("sequence", tokens[i + 1].text, label)
            replace(tokens[i + 1], label)
            i += 2
            braced = i < len(tokens) and tokens[i].text == "{"
            depth = 0
            while i < len(tokens):
                token, low = tokens[i], tokens[i].text.lower()
                if braced:
                    if token.text == "{":
                        depth += 1
                    elif token.text == "}":
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                elif token.text.startswith("$"):
                    break
                if token.text in "{}" or (braced and depth > 1):
                    i += 1
                    continue
                if low == "event":  # old form: event <id> <frame> ["options"]
                    i += 3
                    if i < len(tokens) and tokens[i].quoted:
                        i += 1
                    continue
                if low in _SEQ_OPTIONS:
                    i += _SEQ_OPTIONS[low] + 1
                    continue
                if low.startswith("act_"):
                    i += 2 if i + 1 < len(tokens) and _is_number(tokens[i + 1].text) else 1
                    continue
                if low not in _MOTION and not _is_number(token.text):
                    smd_ref(token)
                i += 1
        else:
            i += 1

    # textures: every material of the meshes, then $texrendermode / $texturegroup
    materials: dict[str, str] = {}  # lower old name -> new file name
    textures = _Unique()
    smd_texts: dict[Path, list[str]] = {}
    for path in smds:
        lines = path.read_text(encoding="latin-1").splitlines()
        smd_texts[path] = lines
        section, count = "", 0
        for line in lines:
            stripped = line.strip()
            if not section:
                if stripped in ("nodes", "skeleton", "triangles"):
                    section, count = stripped, 0
                continue
            if stripped == "end":
                section = ""
                continue
            if section == "triangles":
                if count % 4 == 0 and stripped and stripped.lower() not in materials:
                    stem = stripped[:-4] if stripped.lower().endswith(".bmp") else stripped
                    materials[stripped.lower()] = textures(stem, "tex") + ".bmp"
                count += 1
    def add_texture(name: str) -> None:
        if name.lower() not in materials:
            stem = name[:-4] if name.lower().endswith(".bmp") else name
            materials[name.lower()] = textures(stem, "tex") + ".bmp"

    # textures only $texrendermode / $texturegroup name (other skins) and the
    # rest of the texture folders' BMPs get portable names too
    for k, token in enumerate(tokens):
        if token.text.lower() == "$texrendermode" and k + 1 < len(tokens):
            add_texture(tokens[k + 1].text)
    for directory in dict.fromkeys(texture_dirs):
        if directory.is_dir():
            for bmp in sorted(directory.glob("*")):
                if bmp.is_file() and bmp.suffix.lower() == ".bmp":
                    add_texture(bmp.name)
    for k, token in enumerate(tokens):
        word = token.text.lower()
        if word == "$texrendermode" and k + 1 < len(tokens):
            new = materials.get(tokens[k + 1].text.lower())
            if new:
                replace(tokens[k + 1], new)
        elif word == "$texturegroup":
            j = k + 1
            depth = 0
            while j < len(tokens):
                if tokens[j].text == "{":
                    depth += 1
                elif tokens[j].text == "}":
                    depth -= 1
                    if depth == 0:
                        break
                elif depth >= 2:
                    new = materials.get(tokens[j].text.lower())
                    if new:
                        replace(tokens[j], new)
                j += 1

    # -- write: QC, SMD materials, file renames ------------------------------
    for path, lines in smd_texts.items():
        section, count, changed = "", 0, False
        for n, line in enumerate(lines):
            stripped = line.strip()
            if not section:
                if stripped in ("nodes", "skeleton", "triangles"):
                    section, count = stripped, 0
                continue
            if stripped == "end":
                section = ""
                continue
            if section == "triangles":
                if count % 4 == 0 and stripped:
                    new = materials.get(stripped.lower(), stripped)
                    if new != stripped:
                        lines[n], changed = new, True
                count += 1
        if changed:
            path.write_text("\n".join(lines) + "\n", encoding="latin-1")
    for old, new in materials.items():
        found = next((p for d in texture_dirs if (p := _find_file(d, old)) is not None), None)
        if found is not None and found.name != new:
            _rename(found, found.with_name(new))
            report.note("texture", found.name, new)
    for path, new_rel in file_names.items():
        target = path.with_name(Path(new_rel).name + ".smd")
        if target.name != path.name:
            _rename(path, target)
            report.note("file", path.name, target.name)
    for old_dir in sorted({p.parent for p in file_names}, key=lambda p: -len(p.parts)):
        if old_dir == base or not old_dir.is_relative_to(base):
            continue
        relative = old_dir.relative_to(base)
        new_dir = base.joinpath(*[portable(p) for p in relative.parts])
        if new_dir != old_dir and old_dir.is_dir():
            _rename(old_dir, new_dir)
            report.note("folder", relative.as_posix(), new_dir.relative_to(base).as_posix())
    out = text
    for _start, (token, new) in sorted(edits.items(), reverse=True):
        out = out[:token.start] + f'"{new}"' + out[token.end:]
    if out != text:
        qc_path.write_text(out, encoding="latin-1")
    return report


def _rename(old: Path, new: Path) -> None:
    """Rename, also when only the case changes (case-insensitive disks)."""
    if old == new:
        return
    if new.exists() and not new.samefile(old):
        raise FileExistsError(f"{new} already exists")
    temporary = old.with_name(old.name + ".__renaming")
    old.rename(temporary)
    temporary.rename(new)


__all__ = ["NormalizeReport", "normalize_model", "portable"]
