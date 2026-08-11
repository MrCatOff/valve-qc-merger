"""QC parsing and rewriting (text surgery on the original Crowbar QC).

We only *structurally* need: reference SMDs (body/bodygroup), sequence
SMDs, and every bone reference ($attachment/$hbox/$controller). The output
QC is the original text with hand bodygroups replaced, dead-bone references
re-anchored or dropped, and paths normalized — everything else (events,
fps, flags, textures) passes through untouched, which preserves timing.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


@dataclass
class QcInfo:
    path: str
    text: str
    modelname: str
    references: list[tuple[str, str]]  # (bodygroup/body name, smd base name)
    sequences: list[dict]              # {name, smd, fps}
    attachments: list[dict]            # {idx, bone, offset}
    hboxes: list[str]                  # bone names
    controllers: list[str]             # bone names
    mirrored: list[str] = field(default_factory=list)


_SEQ_BLOCK = re.compile(
    r'\$sequence\s+(?P<name>"[^"]+"|\S+)\s*(?P<body>\{[^}]*\}|\S+[^\n]*)',
    re.IGNORECASE)
_ATTACH = re.compile(
    r'(?im)^\s*\$attachment\s+(?P<idx>\d+)\s+(?P<bone>"[^"]+"|\S+)'
    r'\s+(?P<x>-?[\d.]+)\s+(?P<y>-?[\d.]+)\s+(?P<z>-?[\d.]+)')
_HBOX = re.compile(r'(?im)^\s*\$hbox\s+\d+\s+(?P<bone>"[^"]+"|\S+)')
_CONTROLLER = re.compile(
    r'(?im)^\s*\$controller\s+\S+\s+(?P<bone>"[^"]+"|\S+)')
_BODYGROUP = re.compile(
    r'\$bodygroup\s+(?P<name>"[^"]+"|\S+)\s*\{(?P<inner>[^}]*)\}',
    re.IGNORECASE)
_STUDIO = re.compile(r'studio\s+(?P<studio>"[^"]+"|\S+)', re.IGNORECASE)
_BODY = re.compile(r'(?im)^\s*\$body\s+(?P<name>"[^"]+"|\S+)?\s*'
                   r'studio\s+(?P<studio>"[^"]+"|\S+)')


def _unq(s: str) -> str:
    return s.strip().strip('"')


def parse(path: str) -> QcInfo:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    m = re.search(r'(?im)^\s*\$modelname\s+("[^"]+"|\S+)', text)
    modelname = _unq(m.group(1)) if m else os.path.basename(path)[:-3] + ".mdl"

    references = []
    for bg in _BODYGROUP.finditer(text):
        for st in _STUDIO.finditer(bg.group("inner")):
            references.append((_unq(bg.group("name")),
                               _unq(st.group("studio"))))
    for b in _BODY.finditer(text):
        references.append((_unq(b.group("name") or b.group("studio")),
                           _unq(b.group("studio"))))

    sequences = []
    for sm in _SEQ_BLOCK.finditer(text):
        body = sm.group("body")
        fm = re.search(r'\bfps\s+([\d.]+)', body)
        # the sequence's SMD: first quoted/bare path token in the block
        smd = None
        for tok in re.finditer(r'"([^"]+)"|(?<![{\w])([\w\\/.-]+)', body):
            cand = tok.group(1) or tok.group(2)
            if cand and not cand.startswith("$") and \
                    not re.match(r'^(fps|frame|event|loop|\d)', cand.lower()):
                smd = cand.replace("\\", "/")
                break
        if smd:
            sequences.append({
                "name": _unq(sm.group("name")),
                "smd": smd,
                "fps": float(fm.group(1)) if fm else 30.0,
            })

    attachments = [{
        "idx": int(m.group("idx")),
        "bone": _unq(m.group("bone")),
        "offset": (float(m.group("x")), float(m.group("y")),
                   float(m.group("z"))),
    } for m in _ATTACH.finditer(text)]
    hboxes = [_unq(m.group("bone")) for m in _HBOX.finditer(text)]
    controllers = [_unq(m.group("bone")) for m in _CONTROLLER.finditer(text)]

    return QcInfo(path=os.path.abspath(path), text=text,
                  modelname=modelname, references=references,
                  sequences=sequences, attachments=attachments,
                  hboxes=hboxes, controllers=controllers)


def rewrite(qc: QcInfo, *, drop_studios: set[str],
            attachment_fixes: dict[int, tuple[str, tuple] | None],
            survivors: set[str], modelname: str | None = None,
            hands_studio: str = "hands") -> str:
    """Original text -> output QC text (see module docstring)."""
    text = qc.text.replace("\\", "/")

    def attachment_sub(m):
        idx = int(m.group("idx"))
        if idx not in attachment_fixes:
            return m.group(0)
        fix = attachment_fixes[idx]
        if fix is None:
            return "// (dropped, bone removed) " + m.group(0)
        bone, (x, y, z) = fix
        return '$attachment %d "%s" %.4f %.4f %.4f' % (idx, bone, x, y, z)

    # NOTE: [ \t]* only — \s* would swallow the preceding newline and put
    # the replacement comment on the previous line, leaving the directive
    # alive
    text = re.sub(
        r'(?im)^[ \t]*\$attachment\s+(?P<idx>\d+)\s+("[^"]+"|\S+)'
        r'\s+-?[\d.]+\s+-?[\d.]+\s+-?[\d.]+.*$',
        attachment_sub, text)

    def hbox_sub(m):
        return m.group(0) if _unq(m.group("bone")) in survivors else ""
    text = re.sub(r'(?im)^[ \t]*\$hbox\s+\d+\s+(?P<bone>"[^"]+"|\S+)'
                  r'[^\n]*\n?', hbox_sub, text)

    def controller_sub(m):
        return m.group(0) if _unq(m.group("bone")) in survivors \
            else "// (dropped, bone removed) " + m.group(0)
    text = re.sub(r'(?im)^[ \t]*\$controller\s+\S+\s+(?P<bone>"[^"]+"|\S+)'
                  r'[^\n]*', controller_sub, text)

    if modelname:
        text = re.sub(r'(?im)^(\s*\$modelname\s+)\S+.*$',
                      r'\1"%s"' % modelname, text)
    text = re.sub(r'(?im)^(\s*\$cd\s+)\S+.*$', r'\1"."', text)
    text = re.sub(r'(?im)^(\s*\$cdtexture\s+)\S+.*$', r'\1"."', text)

    hands_block = '$bodygroup "hands"\n{\n\tstudio "%s"\n}\n' % hands_studio
    state = {"replaced": False}

    def swap_or_drop(studio_tok, whole):
        studio = _unq(studio_tok).replace("\\", "/").split("/")[-1].lower()
        if studio in drop_studios:
            if not state["replaced"]:
                state["replaced"] = True
                return hands_block
            return ""
        return whole

    # A hands bodygroup may hold SEVERAL studio lines (a male/female submodel
    # switch, e.g. the pair_deagle fixture), so operate on the whole
    # $bodygroup { ... } block rather than a single studio line.
    def bodygroup_block_sub(m):
        studios = [_unq(s.group("studio")).replace("\\", "/").split("/")[-1]
                   .lower() for s in _STUDIO.finditer(m.group("inner"))]
        if not studios:
            return m.group(0)
        if all(s in drop_studios for s in studios):
            # every submodel is an original hand mesh -> our hands, once
            if not state["replaced"]:
                state["replaced"] = True
                return hands_block
            return ""
        if any(s in drop_studios for s in studios):
            # mixed block: keep the non-hand studio lines, drop the hand ones
            kept = "\n".join(
                line for line in m.group("inner").splitlines()
                if line.strip()
                and not any(d in line.lower() for d in drop_studios))
            return '$bodygroup %s\n{\n%s\n}\n' % (m.group("name"), kept)
        return m.group(0)
    text = re.sub(
        r'\$bodygroup\s+(?P<name>"[^"]+"|\S+)\s*\{(?P<inner>[^}]*)\}\s*\n?',
        bodygroup_block_sub, text)

    def body_sub(m):
        return swap_or_drop(m.group("studio"), m.group(0))
    text = re.sub(r'(?im)^\s*\$body\s+(?:"[^"]+"|\S+)?\s*studio\s+'
                  r'(?P<studio>"[^"]+"|\S+)\s*\n?', body_sub, text)

    if not state["replaced"]:
        # no dedicated hand bodygroup (single-reference models): append ours
        text = re.sub(
            r'((\$bodygroup\s+("[^"]+"|\S+)\s*\{[^}]*\}\s*\n)+)',
            r'\1' + hands_block, text, count=1)
        if hands_block not in text:
            text = re.sub(r'(?im)^(\s*\$body\b[^\n]*\n)',
                          r'\1' + hands_block, text, count=1)
        state["replaced"] = hands_block in text
    if not state["replaced"]:
        raise RuntimeError("could not place the hands bodygroup in the QC")
    return text
