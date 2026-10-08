"""The studio's heavy jobs as plain functions, run in a process of their own.

Each task reopens the project from its folder (the window saved it before
starting the job), does the work with the job's reporter and returns a
picklable result; the window re-reads the project when the job ends. Qt-free:
the job process never loads Qt.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from valve_qc_merger.project import Project, ProjectError
from valve_qc_merger.services.base import Reporter

KIND_TITLES = {
    "v": "View models (v_)",
    "p": "Player-held (p_)",
    "w": "World (w_)",
    "player": "Player bodies",
    "zhands": "Zombie hands",
}


def build(root: str, name: str, then_compile: bool = False, *,
          reporter: Reporter) -> object:
    from valve_qc_merger.studio.build_report import record_part_stats
    project = Project.open(Path(root))
    result = project.run_build(name, reporter)
    reporter.log("measuring output parts…")
    record_part_stats(project.build_dir(name) / "last_run.json", project.root)
    if then_compile:
        if not result.outputs:
            reporter.log("nothing to compile: the build emitted no QC")
            return result
        reporter.check()
        return project.compile_build(name, reporter)
    return result


def plan(root: str, name: str, *, reporter: Reporter) -> object:
    return Project.open(Path(root)).plan_build(name, reporter)


def compile_build(root: str, name: str, force: bool = False, *,
                  reporter: Reporter) -> object:
    return Project.open(Path(root)).compile_build(name, reporter, force=force)


def deploy(root: str, name: str, *, reporter: Reporter) -> object:
    return Project.open(Path(root)).deploy_build(name, reporter)


def derive(root: str, jobs: list[tuple[str, str | None]], mode: str,
           options: dict[str, Any], *, reporter: Reporter) -> dict[str, Any]:
    """Retarget (or another derive mode) every ``(source, name)`` of ``jobs``."""
    project = Project.open(Path(root))
    made: list[str] = []
    failed: list[str] = []
    for done, (source, name) in enumerate(jobs):
        reporter.check()
        reporter.progress(done, len(jobs), source)
        try:
            result, asset = project.derive_asset(source, mode, options, name=name,
                                                 reporter=reporter)
        except ProjectError as exc:
            reporter.log(f"  {source}: {exc}")
            failed.append(source)
            continue
        if asset is None:
            reporter.log(f"  {source}: FAILED (exit {result.exit_code})")
            failed.append(source)
        else:
            reporter.log(f"  + {asset.name}")
            made.append(asset.name)
    reporter.progress(len(jobs), len(jobs), "done")
    if failed:
        reporter.log(f"retarget: {len(failed)} of {len(jobs)} failed: {', '.join(failed)}")
    return {"made": made, "failed": failed}


def import_models(root: str, sources: list[str], category: str | None, mdl: bool, *,
                  reporter: Reporter) -> list[str]:
    """Import .mdl files/folders (``mdl``) or decompiled folders."""
    from valve_qc_merger.project.model import ImportOutcome
    project = Project.open(Path(root))
    paths = [Path(s) for s in sources]
    if mdl:
        outcome = project.import_models(paths, category=category, reporter=reporter)
    else:
        outcome = ImportOutcome()
        for done, source in enumerate(paths):
            reporter.check()
            reporter.progress(done, len(paths), source.name)
            folders = [source] if any(source.glob("*.qc")) else sorted(
                d for d in source.iterdir() if d.is_dir() and any(d.glob("*.qc")))
            for folder in folders:
                if folder.name in project.assets:
                    outcome.skipped.append(folder.name)
                    continue
                try:
                    outcome.added += project.import_decompiled(folder, category=category)
                except (ProjectError, OSError, ValueError) as exc:
                    outcome.failed.append(f"{folder.name}: {exc}")
    for asset in outcome.added:
        where = f"  [{asset.category}]" if asset.category else ""
        reporter.log(f"  + {asset.name:<28} {KIND_TITLES[asset.kind]}{where}")
    if outcome.skipped:
        reporter.log(f"  {len(outcome.skipped)} already in the project, skipped: "
                     + ", ".join(outcome.skipped[:12])
                     + (" …" if len(outcome.skipped) > 12 else ""))
    if outcome.ignored:
        reporter.log(f"  {len(outcome.ignored)} left out (map props, effects, NPCs — not "
                     "weapon or player models): " + ", ".join(outcome.ignored[:12])
                     + (" …" if len(outcome.ignored) > 12 else ""))
    for line in outcome.failed:
        reporter.log(f"  warn: {line}")
    reporter.log(f"  imported {len(outcome.added)} model(s)")
    return [a.name for a in outcome.added]


def import_server(root: str, folder: str, options: dict[str, Any], *,
                  reporter: Reporter) -> str:
    from valve_qc_merger.project.workflow import import_server_folder
    project = Project.open(Path(root))
    result = import_server_folder(project, Path(folder), reporter=reporter, **options)
    for line in result.failed:
        reporter.log(f"  warn: {line}")
    summary = (f"{len(result.models)} model(s), {len(result.sounds)} sound(s), "
               f"{len(result.sprites)} sprite file(s)"
               + (f"; {len(result.skipped)} already here" if result.skipped else ""))
    reporter.log(f"  imported {summary}")
    if result.ignored:
        reporter.log(f"  {len(result.ignored)} model(s) left out — map props, effects, NPCs "
                     "(not weapon or player models): " + ", ".join(result.ignored[:12])
                     + (" …" if len(result.ignored) > 12 else ""))
    if result.game_dir_set:
        reporter.log(f"  game folder set to {folder} (budgets, maps, doctor)")
    return summary


def ping(root: str, seconds: float = 0.0, *, reporter: Reporter) -> str:
    """Prove a job process starts and talks back (the build's selftest);
    ``seconds`` keeps it busy (checking for Cancel) that long."""
    import time
    reporter.log("job process alive")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        reporter.check()
        time.sleep(0.05)
    reporter.progress(1, 1, "ping")
    return f"pong from {Path(root).name}"


TASKS: dict[str, Callable[..., Any]] = {
    "ping": ping, "build": build, "plan": plan, "compile": compile_build, "deploy": deploy,
    "derive": derive, "import_models": import_models, "import_server": import_server,
}


def run(task: str, kwargs: dict[str, Any], reporter: Reporter) -> Any:
    """Run task ``task`` with ``kwargs`` (the job process's entry)."""
    return TASKS[task](reporter=reporter, **kwargs)


__all__ = ["KIND_TITLES", "TASKS", "run"]
