"""Package versions: what changed since the last export, safe new names.

A client downloads a file only when it has none of that name. Update
``models/v_ak47.mdl`` in place and every player who has the old one keeps
it — and a consistency check (ReChecker, ``mp_consistency``) kicks them.
So an export compares itself with the previous one (``vqm_package.json`` in
the package folder) and gives each changed model a new name — ``_v2``,
``_v3``… — with its ``T.mdl`` and ``NN.mdl`` companions (the sequence-group
paths inside the model are rewritten), and changed effect sprites too. The
include, ``.res`` and ReChecker rules then name the new files, and
``update/`` holds only what to upload.

What cannot be renamed — a weapon HUD file (``weapon_*.txt`` is named after
the weapon), its sheets, a sound a compiled model plays — is reported as
changed in place: rename it in the project to reach every player.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path

STATE_FILE = "vqm_package.json"
_COMPANION = re.compile(r"(?i)^(?P<stem>.+?)(?P<tail>t|\d\d)\.mdl$")
_VERSION = re.compile(r"(?i)_v(\d+)$")
SEQGROUP_SIZE = 104  # label[32], name[64], 2 ints


@dataclass
class VersionReport:
    export: int = 1
    added: list[str] = field(default_factory=list)  # published paths
    renamed: dict[str, str] = field(default_factory=dict)  # previous -> new published
    renamed_files: list[str] = field(default_factory=list)  # with their companions
    in_place: list[str] = field(default_factory=list)  # changed, same name
    removed: list[str] = field(default_factory=list)  # in the last export, gone now
    unchanged: int = 0
    first: bool = True  # no previous export to compare with

    @property
    def upload(self) -> list[str]:
        return sorted(self.added + self.renamed_files + self.in_place, key=str.lower)


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def load_state(folder: Path) -> dict | None:
    path = Path(folder) / STATE_FILE
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def groups_of(files: list[str]) -> dict[str, list[str]]:
    """Main path -> its files: a model with its ``T.mdl``/``NN.mdl``
    companions; every other file alone."""
    lower = {f.lower(): f for f in files}
    out: dict[str, list[str]] = {}
    companions = set()
    for path in files:
        match = _COMPANION.match(path)
        if match and f"{match.group('stem')}.mdl".lower() in lower:
            companions.add(path)
    for path in files:
        if path in companions:
            continue
        members = [path]
        if path.lower().endswith(".mdl"):
            stem = path[:-4]
            members += sorted(c for c in companions
                              if _COMPANION.match(c).group("stem").lower() == stem.lower())
        out[path] = members
    return out


def versioned(path: str, version: int) -> str:
    """``models/v_ak47.mdl`` -> ``models/v_ak47_v2.mdl`` (a ``_vN`` already
    there is replaced)."""
    stem, suffix = path.rsplit(".", 1)
    stem = _VERSION.sub("", stem)
    return f"{stem}_v{version}.{suffix}"


def _companion_name(member: str, main: str, new_main: str) -> str:
    return new_main[:-4] + member[len(main) - 4:]


def patch_seqgroups(data: bytes, old_stem: str, new_stem: str) -> bytes:
    """Rewrite the sequence-group file names inside a studio model (they
    name ``<stem>01.mdl``…) from ``old_stem`` to ``new_stem``."""
    if len(data) < 180 or data[:4] != b"IDST":
        return data
    count, index = struct.unpack_from("<2i", data, 172)
    out = bytearray(data)
    old, new = old_stem.lower().encode("latin-1"), new_stem.encode("latin-1")
    for i in range(max(count, 0)):
        offset = index + i * SEQGROUP_SIZE + 32
        if offset + 64 > len(out):
            break
        raw = bytes(out[offset:offset + 64]).split(b"\0", 1)[0]
        name = raw.replace(b"\\", b"/")
        position = name.lower().find(old)
        if position < 0 or not name:
            continue
        renamed = name[:position] + new + name[position + len(old):]
        if len(renamed) < 64:
            out[offset:offset + 64] = renamed.ljust(64, b"\0")
    return bytes(out)


def apply_versions(mod: Path, files: dict[str, int], previous: dict | None,
                   renameable: set[str]) -> tuple[dict[str, str], VersionReport, dict]:
    """Compare the files of ``mod`` (as copied, base names) with the
    ``previous`` export's state; give changed groups whose main file is in
    ``renameable`` new names on disk. Returns base -> published path for
    every file, the report and the new state to save."""
    mod = Path(mod)
    report = VersionReport()
    old_groups: dict[str, dict] = (previous or {}).get("groups", {})
    report.first = previous is None
    report.export = int((previous or {}).get("export", 0)) + 1
    published: dict[str, str] = {}
    state_groups: dict[str, dict] = {}
    for main, members in groups_of(sorted(files)).items():
        digest = hashlib.md5("".join(_md5(mod / m) for m in members).encode()).hexdigest()
        before = old_groups.get(main)
        version = int(before.get("version", 1)) if before else 1
        name = before.get("name", main) if before else main
        if before is None:
            report.added += members
        elif before.get("hash") == digest:
            report.unchanged += len(members)
        elif main in renameable:
            version += 1
            name = versioned(main, version)
            report.renamed[before.get("name", main)] = name
        else:
            report.in_place += members
            name = main
        renamed_now = main in renameable and before is not None and \
            before.get("hash") != digest
        if name != main:
            for member in members:
                target = _companion_name(member, main, name) if member != main else name
                if renamed_now:
                    report.renamed_files.append(target)
                (mod / target).parent.mkdir(parents=True, exist_ok=True)
                data = (mod / member).read_bytes()
                if member == main and len(members) > 1:
                    data = patch_seqgroups(data, Path(main[:-4]).name, Path(name[:-4]).name)
                (mod / member).unlink()
                (mod / target).write_bytes(data)
                published[member] = target
        else:
            for member in members:
                published[member] = member
        state_groups[main] = {"name": name, "hash": digest, "version": version}
    now = set(published.values())
    report.removed = sorted((p for p in (previous or {}).get("files", []) if p not in now),
                            key=str.lower)
    state = {"export": report.export, "groups": state_groups, "files": sorted(now)}
    return published, report, state


def write_update(out: Path, mod: Path, report: VersionReport) -> Path:
    """``update/<mod>/`` with what changed or is new, and ``removed.txt``."""
    update = Path(out) / "update"
    if update.exists():
        shutil.rmtree(update)
    target = update / Path(mod).name
    for path in report.upload:
        (target / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(mod) / path, target / path)
    update.mkdir(parents=True, exist_ok=True)
    (update / "removed.txt").write_text(
        "// files of the previous export that are gone: delete them from the server and "
        "FastDL\n" + "".join(f"{p}\n" for p in report.removed), encoding="utf-8")
    return update


def report_lines(report: VersionReport) -> list[str]:
    if report.first:
        return [f"Export {report.export}: the first one — next time only the changes go to "
                "update/."]
    lines = [f"Export {report.export} compared with the last one: {len(report.added)} new, "
             f"{len(report.renamed)} renamed, {len(report.in_place)} changed in place, "
             f"{len(report.removed)} removed, {report.unchanged} unchanged.",
             "update/ holds what to upload; update/removed.txt what to delete."]
    lines += [f"  renamed {old} -> {new}" for old, new in sorted(report.renamed.items())]
    if report.in_place:
        lines += ["Changed under the same name (players who have the old copy keep it — "
                  "rename it in the project to reach everyone):",
                  *[f"  changed: {p}" for p in report.in_place]]
    return lines


__all__ = ["STATE_FILE", "VersionReport", "apply_versions", "groups_of", "load_state",
           "patch_seqgroups", "report_lines", "versioned", "write_update"]
