"""What state is each asset in? (Explorer badges, Inspector "Status")

Cheap on purpose — it runs on every Explorer refresh over hundreds of
assets: file times, the builds' ``last_run.json`` and the hand check, which
compares geometry (never bone names) but parses only meshes with exactly our
hands' triangle count and caches every verdict by file time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from valve_qc_merger.project.model import Project

_MODEL_RE = re.compile(r"model '([^']+)'")
_RETARGET_RE = re.compile(r"^retarget (\S+) failed")


@dataclass
class AssetStatus:
    hands: str | None = None  # "ours" | "own" (view models only)
    stale: bool = False  # derived, and the source changed since
    builds: list[str] = field(default_factory=list)  # builds that take it
    problems: list[str] = field(default_factory=list)  # "<build>: <failure>"
    replaced: list[str] = field(default_factory=list)  # "<build>: by <asset>"

    @property
    def level(self) -> str:
        """``problem`` > ``stale`` > ``ours`` / ``own`` / ``plain``."""
        if self.problems:
            return "problem"
        if self.stale:
            return "stale"
        return self.hands or "plain"

    def lines(self) -> list[str]:
        out = []
        if self.hands == "ours":
            out.append("on our hands (ready for shared-hands merges)")
        elif self.hands == "own":
            out.append("own hands (not retargeted yet)")
        if self.stale:
            out.append("source changed since this was made: Re-run retarget")
        out += [f"✗ {p}" for p in self.problems]
        out.append(f"in builds: {', '.join(self.builds)}" if self.builds else "in no build")
        out += [f"left out of {r} (same weapon on our hands)" for r in self.replaced]
        return out


def wears_our_hands(directory: Path) -> bool:
    """A reference SMD of the model IS our hand mesh — by geometry in bone
    space, not by bone names (models name bones however they like)."""
    from valve_qc_merger.merge_view.handcheck import dir_wears_hands
    from valve_qc_merger.resources import resource_path
    from valve_qc_merger.retarget.config import DEFAULT_SHARED_HANDS_REFERENCE
    return dir_wears_hands(directory, resource_path(Path(DEFAULT_SHARED_HANDS_REFERENCE)))


HANDS_CACHE = Path(".cache") / "hands.json"  # in the project: asset -> [files, verdict]


def _mesh_signature(directory: Path) -> list[list[object]]:
    """Name, size and time of the model's mesh SMDs (beside its QC): what
    :func:`wears_our_hands` reads, so a verdict holds while it is unchanged."""
    out: list[list[object]] = []
    for path in sorted(directory.glob("*.smd")):
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append([path.name, stat.st_size, stat.st_mtime_ns])
    return out


def _hands_verdicts(project: Project, names: list[str]) -> dict[str, bool]:
    """:func:`wears_our_hands` of each view model, remembered on disk between
    sessions (a 1000-model project parsed every hand mesh on each opening:
    half a minute)."""
    cache_path = project.root / HANDS_CACHE
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    out: dict[str, bool] = {}
    fresh: dict[str, list[object]] = {}
    todo: list[tuple[str, list[list[object]]]] = []
    for name in names:
        signature = _mesh_signature(project.asset_dir(name))
        entry = cache.get(name)
        if isinstance(entry, list) and len(entry) == 2 and entry[0] == signature:
            out[name] = bool(entry[1])
            fresh[name] = [signature, out[name]]
        else:
            todo.append((name, signature))
    if todo:  # not seen before (a new project, an import): on every core
        from valve_qc_merger import workers
        from valve_qc_merger.services.base import Reporter
        results = workers.processes(
            [(_wears_task, (str(project.asset_dir(name)),)) for name, _sig in todo],
            workers=workers.count(project.settings.workers), reporter=Reporter(),
            label="hands")
        for (name, signature), verdict in zip(todo, results, strict=True):
            out[name] = verdict is True
            fresh[name] = [signature, out[name]]
    if fresh != cache:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(fresh), encoding="utf-8")
        except OSError:
            pass  # a read-only project: just slower next time
    return out


def _wears_task(directory: str, *, reporter: object = None) -> bool:
    """:func:`wears_our_hands` in a worker process."""
    return wears_our_hands(Path(directory))


def newest_edit(directory: Path) -> float:
    """Latest modification of the model's QC/SMD files (0 when none)."""
    times = [p.stat().st_mtime for p in directory.rglob("*")
             if p.is_file() and p.suffix.lower() in (".qc", ".smd")]
    return max(times, default=0.0)


def _build_problems(project: Project) -> dict[str, list[str]]:
    """asset name -> failures of the last run of every build naming it."""
    problems: dict[str, list[str]] = {}
    for name in project.builds:
        record_path = project.build_dir(name) / "last_run.json"
        if not record_path.exists():
            continue
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for failure in record.get("failures", []):
            hit = _MODEL_RE.search(failure) or _RETARGET_RE.search(failure)
            if hit and hit.group(1) in project.assets:
                problems.setdefault(hit.group(1), []).append(f"{name}: {failure}")
    return problems


def project_status(project: Project) -> dict[str, AssetStatus]:
    """Status of every asset of ``project``."""
    from valve_qc_merger.project.model import ProjectError

    statuses = {name: AssetStatus() for name in project.assets}
    for build in project.builds.values():
        try:
            chosen = project.chosen_assets(build)
        except ProjectError:
            continue
        superseded = project.superseded_assets(build, chosen)
        for asset in chosen:
            if asset.name in superseded:
                statuses[asset.name].replaced.append(
                    f"{build.name}: by {superseded[asset.name]}")
            else:
                statuses[asset.name].builds.append(build.name)
    for name, problems in _build_problems(project).items():
        statuses[name].problems = problems
    edits: dict[str, float] = {}

    def edited(name: str) -> float:
        if name not in edits:
            edits[name] = newest_edit(project.asset_dir(name))
        return edits[name]

    ours = _hands_verdicts(project, [n for n, a in project.assets.items() if a.kind == "v"])
    for name, asset in project.assets.items():
        status = statuses[name]
        if asset.kind == "v":
            status.hands = "ours" if ours[name] else "own"
        derived = asset.derived
        if derived and derived.get("from") in project.assets:
            made = float(derived.get("at") or 0.0) or edited(name)
            status.stale = edited(derived["from"]) > made + 1.0
    return statuses


__all__ = ["AssetStatus", "project_status", "wears_our_hands"]
