"""Models a merge cannot take, shipped as models of their own.

Every merge (merge-v, merge-p, merge-w) rejects some inputs — a multi-part
weapon under shared hands, a rig it cannot match, a submodel too big for
stock studiomdl, a pose it cannot bake. Dropping them leaves weapons with no
file on the server, so by default each is copied as it is into
``standalone/<name>/`` (its ``$modelname`` set to ``<name>.mdl``), added to
the outputs (compile, deploy, package) and to the manifest with
``standalone = 1`` and the reason. :class:`Rejects` collects them during a
merge; :func:`ship` writes them out.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.merge_view.merger import write_manifest_data
from valve_qc_merger.services.base import Reporter, ServiceResult

_GROUP_RE = re.compile(r'^\s*\$bodygroup\s+"?([^"\s{]+)"?\s*\{(.*?)\}',
                       re.IGNORECASE | re.MULTILINE | re.DOTALL)
_SEQUENCE_RE = re.compile(r'^\s*\$sequence\s+"?([^"\s]+)"?', re.IGNORECASE | re.MULTILINE)


@dataclass
class Rejects:
    """What a merge leaves out, and whether it ships it anyway."""

    enabled: bool
    dirs: dict[str, Path]  # every input model by name
    reasons: dict[str, str] = field(default_factory=dict)

    def reject(self, result: ServiceResult, reporter: Reporter, name: str, message: str,
               tag: str, reason: str) -> None:
        """Ship ``name`` on its own (enabled) or record ``message`` as a failure."""
        if self.enabled and name in self.dirs:
            self.reasons[name] = reason
            result.warnings.append(f"{message} — shipped on its own")
            reporter.log(f"  {name:<20} {tag}  {message} — shipped on its own")
        else:
            result.failures.append(message)
            reporter.log(f"  {name:<20} {tag}  {message}")


def body_layout(qc_text: str) -> list[tuple[str, int, bool]]:
    """``$body`` / ``$bodygroup`` in QC order: (name, entries incl. blank,
    is a hands group) — pev_body strides follow this order."""
    groups: list[tuple[int, str, int, bool]] = []
    for match in re.finditer(r'^\s*\$body\s+"?([^"\s]+)"?', qc_text,
                             re.IGNORECASE | re.MULTILINE):
        groups.append((match.start(), match.group(1), 1, False))
    for match in _GROUP_RE.finditer(qc_text):
        entries = len(re.findall(r"\b(?:studio|blank)\b", match.group(2), re.IGNORECASE))
        groups.append((match.start(), match.group(1), max(entries, 1),
                       "hand" in match.group(1).lower()))
    return [(name, size, hands) for _pos, name, size, hands in sorted(groups)]


def entry(model_dir: Path, name: str, reason: str, *, sequences: bool = True,
          extra: dict[str, object] | None = None) -> dict[str, object]:
    """The manifest entry of a model shipped on its own: ``pev_body`` 0 (the
    first entry of every group), ``hand_stride`` when it has male/female
    hands, and (``sequences``) its own sequence numbers."""
    text = next(Path(model_dir).glob("*.qc")).read_text(encoding="latin-1")
    out: dict[str, object] = {"model": f"{name}.mdl", "pev_body": 0, "standalone": 1,
                              "reason": reason.replace('"', "'"), **(extra or {})}
    stride = 1
    for _group, size, is_hands in body_layout(text):
        if is_hands and size > 1:
            out["hand_stride"] = stride
            break
        stride *= size
    if sequences:
        for index, sequence in enumerate(_SEQUENCE_RE.findall(text)):
            out.setdefault(f"anim_{sequence}", index)
    return out


def merged_manifest(result: ServiceResult) -> dict[str, dict[str, object]]:
    """The manifest the merge wrote (one part: no ``model`` keys, the atlas
    as ``textures``), to which the standalone entries are added."""
    data = {key: dict(value) for key, value in (result.manifest or {}).items()}
    if result.data.get("parts", 0) > 1:
        return data
    single: dict[str, dict[str, object]] = {}
    for key, value in data.items():
        if key.startswith("textures_"):
            single["textures"] = value
        else:
            single[key] = {k: v for k, v in value.items() if k != "model"}
    return single


def ship(rejects: Rejects, out: Path, manifest_format: str, result: ServiceResult,
         reporter: Reporter, *, plan_only: bool = False,
         make_entry: Callable[[Path, str, str], dict[str, object]] | None = None) -> None:
    """Copy every rejected model into ``out/standalone/<name>/`` as it is,
    add its QC to the outputs and its entry to the manifest (or, planning,
    a plan row each)."""
    if not rejects.reasons:
        return
    make_entry = make_entry or (lambda d, n, r: entry(d, n, r))
    entries: dict[str, dict[str, object]] = {}
    rows: list[dict[str, object]] = []
    for name in sorted(rejects.reasons, key=str.lower):
        reporter.check()
        source, reason = rejects.dirs[name], rejects.reasons[name]
        entries[name] = make_entry(source, name, reason)
        rows.append({"part": name, "models": [name], "pev_body": {name: 0}, "folded": [],
                     "pool": "standalone", "bones": None, "standalone": reason,
                     **({"hands": entries[name]["hands"]} if "hands" in entries[name]
                        else {})})
        if plan_only:
            continue
        target = out / "standalone" / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        qc = next(target.glob("*.qc"))
        text = qc.read_text(encoding="latin-1")
        line = f'$modelname "{name}.mdl"'
        if re.search(r"^\s*\$modelname\b", text, re.IGNORECASE | re.MULTILINE):
            text = re.sub(r"^\s*\$modelname\b.*$", line, text, count=1,
                          flags=re.IGNORECASE | re.MULTILINE)
        else:
            text = line + "\n" + text
        final = qc.with_name(f"{name}.qc")
        qc.unlink()
        final.write_text(text, encoding="latin-1")
        result.outputs.append(final)
    if plan_only:
        result.data.setdefault("plan", []).extend(rows)
        return
    manifest = merged_manifest(result)
    manifest.update(entries)
    out.mkdir(parents=True, exist_ok=True)
    write_manifest_data(out, manifest, manifest_format)
    result.manifest = manifest
    own = sum(1 for e in entries.values() if e.get("hands") == "own")
    reporter.log(f"  standalone: {len(entries)} model(s) shipped on their own"
                 + (f" ({own} with their own hands)" if own else ""))


__all__ = ["Rejects", "body_layout", "entry", "merged_manifest", "ship"]
