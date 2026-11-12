"""The whole way from a CSO map to a server's: ``map build``.

For each map: import it with its files (when a source folder is given) ->
check -> fix (CSO classes, missing models, stuck spawns, spawns up to the
profile's count, plugin markers, the WAD list) -> merge its props (when the
profile wants it and studiomdl is found) -> ``.res`` -> check again. Every
step changes only the entity lump: the maps keep their names and CRCs.

A profile (``Rules``; ``configs/mapkit.json``) may add::

    "profiles": {"zp": {"min_spawns": 48, "props": true,
                        "markers": ["zp_supply:24"], "cso_entities": "remove"}}
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.mapkit.analyze import MapReport, check_map, game_folder
from valve_qc_merger.mapkit.fix import FixOptions, plan_fixes, save_plan
from valve_qc_merger.mapkit.rules import Rules
from valve_qc_merger.services.base import Reporter


@dataclass
class BuildOptions:
    out: Path | None = None  # the game folder; default each map's own
    sources: list[Path] = field(default_factory=list)  # unpacked CSO folders to import from
    folders: list[Path] = field(default_factory=list)  # the base game (--game)
    studiomdl: Path | None = None
    props: bool | None = None  # None: the profile's ("props", default true)
    overwrite: bool = False


@dataclass
class MapBuild:
    name: str
    before: MapReport | None = None
    after: MapReport | None = None
    steps: list[str] = field(default_factory=list)
    error: str = ""
    trace: str = ""  # the traceback of ``error`` (the JSON report keeps it)

    def to_dict(self) -> dict[str, object]:
        return {"map": self.name, "steps": self.steps, "error": self.error, "trace": self.trace,
                "before": self.before.to_dict() if self.before else None,
                "after": self.after.to_dict() if self.after else None}


def _markers(rules: Rules) -> list[tuple[str, int]]:
    out = []
    raw = rules.extra.get("markers", [])
    for item in raw if isinstance(raw, list) else []:
        name, _, count = str(item).partition(":")
        if name and count.isdigit():
            out.append((name, int(count)))
    return out


def build_maps(maps: list[str], rules: Rules, options: BuildOptions,
               reporter: Reporter | None = None) -> list[MapBuild]:
    """Run the pipeline (see the module) over ``maps`` — BSP paths, or map
    names in ``options.sources``."""
    from valve_qc_merger.mapkit.nexon import ImportResult, SourceIndex, import_map, write_record
    from valve_qc_merger.mapkit.props import find_studiomdl, merge_props

    reporter = reporter or Reporter()
    results: list[MapBuild] = []
    index = SourceIndex.build(options.sources) if options.sources else None
    imported = ImportResult()
    want_props = options.props if options.props is not None else bool(
        rules.extra.get("props", True))
    studiomdl = find_studiomdl(options.studiomdl) if want_props else None
    for done, item in enumerate(maps):
        reporter.check()
        reporter.progress(done, len(maps), item)
        build = MapBuild(Path(item).stem)
        results.append(build)
        path = Path(item)
        try:
            if index is not None:
                out = options.out or (game_folder(path) if path.suffix == ".bsp" else None)
                if out is None:
                    raise ValueError("--out is needed to import from a source folder")
                import_map(item, index, out, base=options.folders,
                           overwrite=options.overwrite, result=imported)
                path = out / "maps" / f"{build.name.lower()}.bsp"
                if not path.is_file():
                    raise ValueError(f"{item} is not in the source")
                build.steps.append("imported with its files")
            elif options.out is not None:
                target = options.out / "maps" / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.resolve() != path.resolve():
                    target.write_bytes(path.read_bytes())
                path = target
            build.before = check_map(path, options.folders, rules)
            fix_options = FixOptions(cso_entities=str(rules.extra.get("cso_entities", "remove")),
                                     markers=_markers(rules), sources=options.sources)
            plan = plan_fixes(build.before, fix_options, rules)
            if plan.active:
                save_plan(plan, folders=options.folders, backup=options.out is None)
                build.steps.append(f"fixed: {len(plan.active)} entity changes")
            build.steps += [f"note: {n}" for n in plan.notes]
            if want_props and studiomdl is not None:
                game = game_folder(path)
                merged = merge_props([path], game, f"{path.stem}_props",
                                     folders=options.folders, studiomdl=studiomdl)
                if merged.parts and merged.plans and merged.plans[0].active:
                    save_plan(merged.plans[0], folders=options.folders, backup=False)
                    build.steps.append(f"props: {len(merged.merged)} models -> "
                                       f"{len(merged.parts)} merged, model slots "
                                       f"{merged.slots_before} -> {merged.slots_after}")
                build.steps += [f"props apart: {m.path} — {m.reason}" for m in merged.apart]
            elif want_props:
                build.steps.append("props: not merged (no studiomdl)")
            build.after = check_map(path, options.folders, rules)
        except Exception as exc:  # noqa: BLE001 - one map's failure does not stop the rest
            build.error = f"{type(exc).__name__}: {exc}"
            build.trace = traceback.format_exc()
        reporter.log(f"{build.name}: " + (build.error or (build.after.level.upper()
                                                          if build.after else "")))
    if index is not None and options.out is not None:
        write_record(imported, options.out)
    reporter.progress(len(maps), len(maps), "done")
    return results


def write_report(results: list[MapBuild], path: Path) -> Path:
    path.write_text(json.dumps([r.to_dict() for r in results], indent=1, ensure_ascii=False),
                    encoding="utf-8")
    return path


__all__ = ["BuildOptions", "MapBuild", "build_maps", "write_report"]
