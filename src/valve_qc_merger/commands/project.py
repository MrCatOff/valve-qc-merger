"""CLI wiring for ``project``: a Studio project without the window — import,
run, compile, deploy and package its builds from a script, CI or a server.

    valve-qc-merger project info    PROJECT
    valve-qc-merger project build   PROJECT [BUILD ...] [--compile] [--deploy]
    valve-qc-merger project compile PROJECT [BUILD ...] [--deploy]
    valve-qc-merger project deploy  PROJECT [BUILD ...]
    valve-qc-merger project package PROJECT --out FOLDER [BUILD ...]
    valve-qc-merger project import  PROJECT SOURCE ... [--decompiled]
    valve-qc-merger project import-server PROJECT CSTRIKE

No BUILD names: every build of the project. ``--studiomdl`` / ``--game-dir``
override the project's settings for this run only (nothing is saved).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from valve_qc_merger.commands.base import Command
from valve_qc_merger.services.base import EXIT_DISCOVERY, EXIT_FAIL, EXIT_OK, Reporter


def _open(args: argparse.Namespace):  # noqa: ANN202 - Project, imported lazily
    from valve_qc_merger.project import Project
    project = Project.open(args.project)
    if getattr(args, "studiomdl", None):
        project.settings.studiomdl = str(args.studiomdl)
    if getattr(args, "game_dir", None):
        project.settings.game_dir = str(args.game_dir)
    return project


def _builds(project, names: list[str]) -> list[str]:  # noqa: ANN001
    from valve_qc_merger.project import ProjectError
    unknown = [n for n in names if n not in project.builds]
    if unknown:
        raise ProjectError(f"no build {', '.join(unknown)} (has: "
                           f"{', '.join(sorted(project.builds)) or 'none'})")
    return names or sorted(project.builds)


def _status(project, name: str) -> str:  # noqa: ANN001
    from valve_qc_merger.services.compile import compiled_model_path
    record_path = project.build_dir(name) / "last_run.json"
    if not record_path.exists():
        return "not run"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record.get("exit_code"):
        return f"last run failed (exit {record['exit_code']})"
    outputs = record.get("outputs", [])
    compiled = sum(compiled_model_path(project.root / q).exists() for q in outputs)
    text = f"run: {len(outputs)} QC, {compiled} compiled"
    if project.server_tree(name).is_dir():
        text += ", server files ready"
    return text


class ProjectCommand(Command):
    """Work with a Studio project from the command line."""

    name = "project"
    help = ("a Studio project without the window: info, build, compile, deploy, package, "
            "import")

    def configure(self, parser: argparse.ArgumentParser) -> None:
        actions = parser.add_subparsers(dest="action", metavar="<action>", required=True)

        def action(name: str, text: str, builds: bool = True) -> argparse.ArgumentParser:
            sub = actions.add_parser(name, help=text, description=text)
            sub.add_argument("project", type=Path, help="the project folder (project.toml)")
            if builds:
                sub.add_argument("builds", nargs="*", metavar="BUILD",
                                 help="build names (default: every build)")
            return sub

        action("info", "list the project's assets and builds with their state", builds=False)
        build = action("build", "run builds (merge), optionally compile and deploy them")
        build.add_argument("--compile", action="store_true", help="compile after the run")
        build.add_argument("--deploy", action="store_true",
                           help="deploy after compiling (implies --compile)")
        compile_ = action("compile", "compile builds' last run with studiomdl")
        compile_.add_argument("--deploy", action="store_true", help="deploy after compiling")
        action("deploy", "copy builds' server files (builds/<name>/cstrike) into the "
                         "game folder")
        package = action("package", "export a server package (cstrike tree, AMXX include, "
                                    ".res, ReChecker rules, update set)")
        package.add_argument("--out", type=Path, required=True, help="package folder")
        for sub in (build, compile_):
            sub.add_argument("--studiomdl", type=Path, help="studiomdl to use this time")
            sub.add_argument("--force", action="store_true",
                             help="compile every model (not only those whose sources "
                                  "changed)")
        for sub in (build, compile_, actions.choices["deploy"]):
            sub.add_argument("--game-dir", type=Path,
                             help="the game's mod folder (…/cstrike) to use this time")
        imp = action("import", "import .mdl files/folders (or decompiled folders)",
                     builds=False)
        imp.add_argument("sources", nargs="+", type=Path, metavar="SOURCE")
        imp.add_argument("--decompiled", action="store_true",
                         help="sources are decompiled folders (QC + SMD), not .mdl")
        imp.add_argument("--category", help="put the new assets in this category")
        server = action("import-server", "bring a server's mod folder in: weapon and "
                                          "player models, the sounds they play, weapon HUDs",
                        builds=False)
        server.add_argument("folder", type=Path, help="the server's …/cstrike folder")
        server.add_argument("--sounds", choices=("used", "all", "none"), default="used")
        server.add_argument("--category", help="put the new assets in this category")

    def run(self, args: argparse.Namespace) -> int:
        from valve_qc_merger.project import ProjectError
        try:
            return getattr(self, "_" + args.action.replace("-", "_"))(args)
        except ProjectError as exc:
            print(f"error: {exc}")
            return EXIT_DISCOVERY

    # -- actions ---------------------------------------------------------------
    def _info(self, args: argparse.Namespace) -> int:
        project = _open(args)
        kinds: dict[str, int] = {}
        for asset in project.assets.values():
            kinds[asset.kind] = kinds.get(asset.kind, 0) + 1
        print(f"{project.name}  ({project.root})")
        print("assets: " + (", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
                            or "none"))
        print(f"studiomdl: {project.settings.studiomdl or '-'}")
        print(f"game folder: {project.settings.game_dir or '-'}")
        for name in sorted(project.builds):
            build = project.builds[name]
            print(f"  {name:<24} {build.kind:<14} {_status(project, name)}")
        return EXIT_OK

    def _build(self, args: argparse.Namespace) -> int:
        project = _open(args)
        worst = EXIT_OK
        for name in _builds(project, args.builds):
            print(f"== build {name}")
            result = project.run_build(name, Reporter())
            if not result.ok:
                print(f"build {name} failed (exit {result.exit_code})")
                worst = max(worst, result.exit_code)
                continue
            if args.compile or args.deploy:
                worst = max(worst, self._compile_one(project, name, args.deploy, args.force))
        return worst

    def _compile_one(self, project, name: str, deploy: bool,  # noqa: ANN001
                     force: bool = False) -> int:
        print(f"== compile {name}")
        result = project.compile_build(name, Reporter(), force=force)
        if not result.ok:
            for failure in result.failures:
                print(f"  failed: {failure}")
            return result.exit_code or EXIT_FAIL
        if deploy and not project.settings.deploy_after_compile:
            return self._deploy_one(project, name)
        return EXIT_OK

    def _compile(self, args: argparse.Namespace) -> int:
        project = _open(args)
        return max([self._compile_one(project, name, args.deploy, args.force)
                    for name in _builds(project, args.builds)], default=EXIT_OK)

    def _deploy_one(self, project, name: str) -> int:  # noqa: ANN001
        print(f"== deploy {name}")
        return project.deploy_build(name, Reporter()).exit_code

    def _deploy(self, args: argparse.Namespace) -> int:
        project = _open(args)
        return max([self._deploy_one(project, name)
                    for name in _builds(project, args.builds)], default=EXIT_OK)

    def _package(self, args: argparse.Namespace) -> int:
        from valve_qc_merger.server.package import export_package, report
        project = _open(args)
        result = export_package(project, args.out, _builds(project, args.builds))
        print(report(result))
        print(f"package written to {args.out}")
        return EXIT_FAIL if result.skipped_builds and args.builds else EXIT_OK

    def _import(self, args: argparse.Namespace) -> int:
        from valve_qc_merger.studio.tasks import import_models
        project = _open(args)
        import_models(str(project.root), [str(s) for s in args.sources], args.category,
                      not args.decompiled, reporter=Reporter())
        return EXIT_OK

    def _import_server(self, args: argparse.Namespace) -> int:
        from valve_qc_merger.studio.tasks import import_server
        project = _open(args)
        options = {"sounds": args.sounds, "category": args.category}
        print(import_server(str(project.root), str(args.folder), options, reporter=Reporter()))
        return EXIT_OK


__all__ = ["ProjectCommand"]
