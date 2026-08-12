"""Adopt the donor's canonical sequence set; void unneeded slots with a placeholder.

CS 1.6 selects player animations by sequence index/activity, so the donor's
sequence *order* must be preserved exactly for in-game playback. We copy the
donor's ``$sequence`` blocks verbatim (blends, fps, loop, events all intact),
only rewriting animation paths to the merged ``anims/`` folder. Sequences the
server does not need (shields, by default) keep their slot but have their body
swapped for the tiny ``I_am_a_stupid_placeholder`` SMD — the same trick the
donor itself uses — dropping the heavy blend data without shifting any index.
CSO animations are ignored entirely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from valve_qc_merger.merge_players.discovery import Donor
from valve_qc_merger.models.geometry import Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Smd
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.retarget.qc_build import _matching_brace
from valve_qc_merger.writers.smd import write_smd_text

PLACEHOLDER_STEM = "I_am_a_stupid_placeholder"
DEFAULT_PLACEHOLDER_GLOBS = ("*shield*",)

_SEQ_KW_RE = re.compile(r"\$sequence\b")
_TOKEN_RE = re.compile(r'\s*(?:"(?P<q>[^"]+)"|(?P<w>[^\s{}"]+))')
_QUOTED_PATH_RE = re.compile(r'^"([^"]+)"$')
_FPS_RE = re.compile(r"\bfps\s+([0-9.]+)")


@dataclass
class SequencePlan:
    qc_lines: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    placeholdered: list[str] = field(default_factory=list)
    copied: int = 0
    warnings: list[str] = field(default_factory=list)


def _split_token(path: str) -> tuple[str, str]:
    """``arctic_anims\\ref_aim`` -> (``arctic_anims``, ``ref_aim``)."""
    parts = path.replace("\\", "/").split("/")
    return "/".join(parts[:-1]), parts[-1]


def _rebase_positions(smd: Smd, bind: dict[str, Vector3]) -> Smd:
    """Set every non-root bone's position to the group bind (keep rotations).

    GoldSrc bakes bone POSITIONS (lengths) into the compiled animation, so a
    donor animation would impose the DONOR's proportions on every mesh (the
    stretch). Rebasing non-root positions to the target group's own bind lengths
    makes the animation carry only rotations; each mesh keeps its proportions.
    The root keeps its per-frame position (locomotion).
    """
    name_of = {n.index: n.name for n in smd.nodes}
    roots = {n.index for n in smd.nodes if n.parent < 0}
    smd.frames = [
        Frame(f.time, tuple(
            p if p.bone in roots or name_of.get(p.bone) not in bind
            else BonePose(p.bone, bind[name_of[p.bone]], p.rotation)
            for p in f.poses
        ))
        for f in smd.frames
    ]
    return smd


def _emit_smd(src: Path, dst: Path, bind: dict[str, Vector3] | None) -> bool:
    """Write an anim SMD to ``dst``: rebased to ``bind`` if given, else copied (LF)."""
    if not src.exists():
        return False
    if dst.exists():
        return True
    if bind is None:
        dst.write_bytes(src.read_bytes().replace(b"\r\n", b"\n"))
    else:
        dst.write_text(write_smd_text(_rebase_positions(parse_smd_file(src), bind)),
                       encoding="latin-1")
    return True


def build_sequences(
    donor: Donor,
    out_dir: Path,
    *,
    placeholder_globs: tuple[str, ...] = DEFAULT_PLACEHOLDER_GLOBS,
    anims_subdir: str = "anims",
    bind_positions: dict[str, Vector3] | None = None,
) -> SequencePlan:
    """Emit the donor's sequence blocks (paths rewritten) and copy/rebase their SMDs.

    With ``bind_positions`` (the target group's bind), each anim is rebased so it
    carries only rotations — the meshes keep their own proportions (no stretch).
    """
    plan = SequencePlan()
    anims_out = out_dir / anims_subdir
    anims_out.mkdir(parents=True, exist_ok=True)
    text = donor.qc_text
    donor_anim_dirs: list[str] = []

    for kw in _SEQ_KW_RE.finditer(text):
        name_m = _TOKEN_RE.match(text, kw.end())
        if name_m is None:
            continue
        name = name_m.group("q") or name_m.group("w")
        cursor = name_m.end()
        while cursor < len(text) and text[cursor] in " \t":
            cursor += 1
        if cursor < len(text) and text[cursor] == "{":
            body = text[cursor + 1:_matching_brace(text, cursor)]
        else:
            end = text.find("\n", cursor)
            body = text[cursor:len(text) if end == -1 else end]

        plan.names.append(name)
        placeholder = any(fnmatch(name, g) for g in placeholder_globs)

        if placeholder:
            plan.placeholdered.append(name)
            fps_m = _FPS_RE.search(body)
            plan.qc_lines.append(f'$sequence "{name}" {{')
            plan.qc_lines.append(f'\t"{anims_subdir}/{PLACEHOLDER_STEM}"')
            if fps_m:
                plan.qc_lines.append(f"\tfps {fps_m.group(1)}")
            if re.search(r"\bloop\b", body):
                plan.qc_lines.append("\tloop")
            plan.qc_lines.append("}")
            continue

        # Kept verbatim: rewrite each source path to anims/<stem>, copy its SMD.
        plan.qc_lines.append(f'$sequence "{name}" {{')
        for raw in body.splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            path_m = _QUOTED_PATH_RE.match(stripped)
            if path_m:
                src_dir, stem = _split_token(path_m.group(1))
                if src_dir:
                    donor_anim_dirs.append(src_dir)
                if _emit_smd(donor.directory / src_dir / f"{stem}.smd",
                             anims_out / f"{stem}.smd", bind_positions):
                    plan.copied += 1
                else:
                    plan.warnings.append(f"sequence {name!r}: missing anim {stem}.smd")
                plan.qc_lines.append(f'\t"{anims_subdir}/{stem}"')
            else:
                plan.qc_lines.append(f"\t{stripped}")
        plan.qc_lines.append("}")

    # Ensure the placeholder SMD is present whenever any slot was voided.
    if plan.placeholdered:
        anim_dir = max(set(donor_anim_dirs), key=donor_anim_dirs.count) \
            if donor_anim_dirs else f"{donor.directory.name}_anims"
        if not _emit_smd(donor.directory / anim_dir / f"{PLACEHOLDER_STEM}.smd",
                         anims_out / f"{PLACEHOLDER_STEM}.smd", bind_positions):
            plan.warnings.append(
                f"placeholder SMD {PLACEHOLDER_STEM}.smd not found in donor "
                f"{anim_dir!r}; voided sequences will not compile"
            )
    return plan


__all__ = ["build_sequences", "SequencePlan", "PLACEHOLDER_STEM",
           "DEFAULT_PLACEHOLDER_GLOBS"]
