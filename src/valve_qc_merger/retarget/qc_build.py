"""Generate a compilable QC for the exported model (Phase 5 convenience).

Beyond §7.7 (which lists only SMD outputs), a GoldSrc model still needs a QC to
compile. This rebuilds one from the input QC: the same sequences (fps + events)
and attachments, but pointing at the *merged* reference-hands+weapon mesh SMD and
the retargeted ``anims/`` set. References to bones that Phase 5 deletes (the
original arm/forearm bones used by ``$hbox``) are dropped, since those bones no
longer exist in the unified skeleton.

Pure text in, pure text out — no ``bpy``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SEQ_KW_RE = re.compile(r"\$sequence\b")
# A QC token: a quoted string or a bare word (studiomdl and every decompiler
# accept sequence/bodygroup names with or without quotes).
_TOKEN_RE = re.compile(r'\s*(?:"(?P<q>[^"]+)"|(?P<w>[^\s{}"]+))')
_FPS_RE = re.compile(r"\bfps\s+(?P<fps>[0-9.]+)")
_EVENT_RE = re.compile(r"\{\s*event\b[^}]*\}")
_ATTACH_RE = re.compile(
    r'\$attachment\s+(?P<idx>\d+)\s+"(?P<bone>[^"]+)"\s+'
    r"(?P<x>\S+)\s+(?P<y>\S+)\s+(?P<z>\S+)"
)
_MODELNAME_RE = re.compile(r'\$modelname\s+"(?P<name>[^"]+)"')
_SEQ_SMD_RE = re.compile(r'"(?P<path>[^"{}]+)"')  # first quoted token outside events
_TEXRENDERMODE_RE = re.compile(
    r'\$texrendermode\s+"(?P<tex>[^"]+)"\s+(?P<mode>\w+)'
)


@dataclass(frozen=True)
class QcSequence:
    name: str
    fps: float | None
    events: tuple[str, ...]
    smd: str | None = None  # animation SMD path as parsed from the QC (either separator)


def _matching_brace(text: str, open_index: int) -> int:
    """Index of the ``}`` that closes the ``{`` at ``open_index`` (nesting-aware)."""
    depth = 0
    for i in range(open_index, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(text)


def parse_sequences(qc_text: str) -> list[QcSequence]:
    """Extract every ``$sequence`` with its fps and raw event lines, in file order.

    Handles both dialects: our/Crowbar style ``$sequence "name" { ... }`` and the
    decompiler's ``$sequence name "./anims/name" fps 16`` (bare name, single line).
    A braced body is delimited by brace matching (it may nest ``{ event ... }``);
    an unbraced body runs to the end of the line.
    """
    out: list[QcSequence] = []
    for kw in _SEQ_KW_RE.finditer(qc_text):
        name_m = _TOKEN_RE.match(qc_text, kw.end())
        if name_m is None:
            continue
        name = name_m.group("q") or name_m.group("w")
        cursor = name_m.end()
        while cursor < len(qc_text) and qc_text[cursor] in " \t":
            cursor += 1
        if cursor < len(qc_text) and qc_text[cursor] == "{":
            body = qc_text[cursor + 1:_matching_brace(qc_text, cursor)]
        else:
            end = qc_text.find("\n", cursor)
            body = qc_text[cursor:len(qc_text) if end == -1 else end]
        fps_m = _FPS_RE.search(body)
        fps = float(fps_m.group("fps")) if fps_m else None
        events = tuple(e.strip() for e in _EVENT_RE.findall(body))
        smd_m = _SEQ_SMD_RE.search(_EVENT_RE.sub("", body))
        smd = smd_m.group("path") if smd_m else None
        out.append(QcSequence(name, fps, events, smd))
    return out


_BODYGROUP_RE = re.compile(r'\$bodygroup\s+(?:"(?P<name>[^"]+)"|(?P<bare>[^\s{}"]+))\s*\{')
_STUDIO_RE = re.compile(r'studio\s+"(?P<stem>[^"]+)"')


def parse_bodygroups(qc_text: str) -> dict[str, list[str]]:
    """Every ``$bodygroup`` block's studio stems, in file order (QC discovery).

    Decompilers emit DUPLICATE group names (e.g. two ``weapon`` groups for a
    submodel split); those are kept as ``name``, ``name_2``, ``name_3``, ... —
    a dict keyed by raw name would silently drop all but the last block.
    """
    out: dict[str, list[str]] = {}
    for m in _BODYGROUP_RE.finditer(qc_text):
        open_index = m.end() - 1
        close_index = _matching_brace(qc_text, open_index)
        body = qc_text[open_index + 1:close_index]
        raw = m.group("name") or m.group("bare")
        name = raw
        counter = 2
        while name in out:
            name = f"{raw}_{counter}"
            counter += 1
        out[name] = [s.group("stem") for s in _STUDIO_RE.finditer(body)]
    return out


def parse_attachments(qc_text: str, surviving_bones: set[str]) -> list[str]:
    """Return ``$attachment`` lines whose bone survives into the unified skeleton."""
    kept: list[str] = []
    for m in _ATTACH_RE.finditer(qc_text):
        if m.group("bone") in surviving_bones:
            kept.append(
                f'$attachment {m.group("idx")} "{m.group("bone")}" '
                f'{m.group("x")} {m.group("y")} {m.group("z")}'
            )
    return kept


def modelname(qc_text: str, default: str) -> str:
    m = _MODELNAME_RE.search(qc_text)
    return m.group("name") if m else default


def parse_texrendermodes(qc_text: str) -> list[str]:
    """Return the source ``$texrendermode`` lines (additive/masked glow effects).

    Effect meshes — a muzzle flash, blood glass, additive fx — render wrong
    without their rendermode. Carried verbatim; the texture names studiomdl sees
    match (:func:`finalize_textures` only sanitises spaces/extensions, which these
    effect BMPs do not have)."""
    return [
        f'$texrendermode "{m.group("tex")}" {m.group("mode")}'
        for m in _TEXRENDERMODE_RE.finditer(qc_text)
    ]


def build_qc(
    qc_text: str,
    *,
    mesh_stem: str,
    anims_subdir: str,
    surviving_bones: set[str],
    model_name: str,
    hand_bodies: list[str] | None = None,
    weapon_bodies: list[str] | None = None,
) -> str:
    """Render a QC that compiles the exported mesh SMDs + retargeted animations.

    ``weapon_bodies`` lists every weapon-part SMD stem (default: ``[mesh_stem]``).
    A weapon split across always-on parts (bloodhunter: pistol + blood projectile
    + effects) emits one ``$bodygroup "weapon"`` per part so each stays a separate
    submodel. With ``hand_bodies`` (exported hand-variant SMD stems) a
    ``$bodygroup "hands"`` lists every variant — the in-game selectable hand
    meshes on one shared skeleton. Without hand variants the merged mesh (hands
    folded into the first weapon part) compiles as one ``$body``.
    """
    sequences = parse_sequences(qc_text)
    attachments = parse_attachments(qc_text, surviving_bones)
    texrendermodes = parse_texrendermodes(qc_text)
    weapons = weapon_bodies or [mesh_stem]

    if hand_bodies:
        body_lines = []
        for weapon in weapons:
            body_lines += ['$bodygroup "weapon"', "{", f'\tstudio "{weapon}"', "}"]
        body_lines += [
            '$bodygroup "hands"',
            "{",
            *[f'\tstudio "{body}"' for body in hand_bodies],
            "}",
        ]
        header = "// unified skeleton; weapon and hand variants as bodygroups."
    else:
        # No hand variants: the reference hands are merged into the first weapon
        # part's SMD ($body); any further parts stay their own always-on bodygroup.
        body_lines = [f'$body "studio" "{weapons[0]}"']
        for weapon in weapons[1:]:
            body_lines += ['$bodygroup "weapon"', "{", f'\tstudio "{weapon}"', "}"]
        header = "// unified skeleton. Hands and weapon are merged into one reference SMD."

    lines: list[str] = [
        "// Regenerated by valve-qc-merger (Phase 5): reference hands + weapon,",
        header,
        "",
        f'$modelname "{modelname(qc_text, model_name)}"',
        '$cd "."',
        '$cdtexture "."',
        "$cliptotextures",
        "$scale 1.0",
        "",
        *body_lines,
        "",
        "$flags 0",
        "",
    ]
    for mode in texrendermodes:
        lines.append(mode)
    if texrendermodes:
        lines.append("")
    for attach in attachments:
        lines.append(attach)
    if attachments:
        lines.append("")

    for seq in sequences:
        lines.append(f'$sequence "{seq.name}" {{')
        # Forward slash: GoldSrc studiomdl accepts it on Windows and it is the
        # only separator the native macOS studiomdl port resolves (a backslash
        # reads as a literal filename char there -> "anims\idle.smd doesn't exist").
        lines.append(f'\t"{anims_subdir}/{seq.name}"')
        for event in seq.events:
            lines.append(f"\t{event}")
        if seq.fps is not None:
            fps = int(seq.fps) if seq.fps == int(seq.fps) else seq.fps
            lines.append(f"\tfps {fps}")
        lines.append("}")
    lines.append("")
    return "\n".join(lines)


__all__ = ["QcSequence", "build_qc", "parse_sequences", "parse_attachments",
           "parse_bodygroups", "parse_texrendermodes", "modelname"]
