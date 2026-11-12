"""CLI wiring for ``map``: check GoldSource maps before they go on a server.

    valve-qc-merger map check   MAP.bsp ... [--game DIR] [--json OUT] [--profile zp]
    valve-qc-merger map preview MAP.bsp --out plan.png [--size 1024] [--no-spawns]

``--game`` adds a game folder the map's files are looked up in (a clean
``cstrike``; ``valve`` next to it is searched too). Exit codes of
``check``: 0 no issues, 1 warnings, 2 critical issues, 3 a map could not be
read. The work lives in :mod:`valve_qc_merger.mapkit`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from valve_qc_merger.commands.base import Command

EXIT_CLEAN, EXIT_WARNINGS, EXIT_CRITICAL, EXIT_UNREADABLE = 0, 1, 2, 3


def _maps(sources: list[Path]) -> list[Path]:
    out: list[Path] = []
    for source in sources:
        if source.is_dir():
            out += sorted(p for p in source.rglob("*") if p.suffix.lower() == ".bsp")
        else:
            out.append(source)
    return out


class MapCommand(Command):
    """Check and preview maps."""

    name = "map"
    help = "check GoldSource maps (BSP v30): limits, missing files, spawns, textures; plans"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        actions = parser.add_subparsers(dest="action", metavar="<action>", required=True)
        check = actions.add_parser("check", help="report what would break a map on a server",
                                   description="report what would break a map on a server")
        check.add_argument("maps", nargs="+", type=Path, metavar="MAP",
                           help="BSP files or folders searched for them")
        check.add_argument("--game", type=Path, action="append", default=[], metavar="DIR",
                           help="a game folder to look files up in (repeatable)")
        check.add_argument("--json", type=Path, metavar="OUT",
                           help="write the report(s) as JSON")
        check.add_argument("--profile", help="the rules profile (default: zp)")
        check.add_argument("--rules", type=Path, metavar="FILE",
                           help="a rules file (JSON, see configs/mapkit.json)")
        check.add_argument("--all", action="store_true", help="also print info lines")
        preview = actions.add_parser("preview", help="draw a map from above (PNG)",
                                     description="draw a map from above (PNG)")
        preview.add_argument("map", type=Path, metavar="MAP")
        preview.add_argument("--out", type=Path, required=True, help="the PNG to write")
        preview.add_argument("--size", type=int, default=1024, help="longest side, pixels")
        preview.add_argument("--game", type=Path, action="append", default=[], metavar="DIR",
                             help="a game folder holding the map's WADs (repeatable)")
        preview.add_argument("--no-spawns", action="store_true", help="leave the spawns out")
        preview.add_argument("--zmin", type=float, help="only floors at this height or above")
        preview.add_argument("--zmax", type=float, help="only floors at this height or below")

    def run(self, args: argparse.Namespace) -> int:
        if args.action == "check":
            return self._check(args)
        return self._preview(args)

    def _check(self, args: argparse.Namespace) -> int:
        from valve_qc_merger.mapkit.analyze import check_maps
        from valve_qc_merger.mapkit.rules import Rules

        rules = Rules.load(args.rules, args.profile)
        paths = _maps(args.maps)
        if not paths:
            print("no .bsp files found")
            return EXIT_UNREADABLE
        results = check_maps(paths, args.game, rules)
        code = EXIT_CLEAN
        dumped = []
        for path, result in zip(paths, results, strict=True):
            if isinstance(result, Exception):
                print(f"{path.name}: {result}")
                code = EXIT_UNREADABLE
                continue
            dumped.append(result.to_dict())
            s = result.spawns
            stuck = f" ({s['bad']} stuck)" if s.get("bad") else ""
            print(f"{result.map}: {result.level.upper()}  spawns CT {s['ct']} / T {s['t']}{stuck}"
                  f"  model slots {result.slots['total']} (free {result.slots['free_models']})"
                  f"  sounds {result.slots['sounds']}")
            for issue in result.issues:
                if issue.level != "info" or args.all:
                    print(f"  {issue.level:<8} {issue.code:<18} {issue.text}")
            if code != EXIT_UNREADABLE:
                level = {"critical": EXIT_CRITICAL, "warning": EXIT_WARNINGS}.get(result.level,
                                                                                 EXIT_CLEAN)
                code = max(code, level)
        if args.json:
            args.json.write_text(json.dumps(dumped[0] if len(dumped) == 1 else dumped,
                                            indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"report: {args.json}")
        return code

    def _preview(self, args: argparse.Namespace) -> int:
        from valve_qc_merger.mapkit.analyze import map_wads
        from valve_qc_merger.mapkit.bsp import Bsp, BspError
        from valve_qc_merger.mapkit.render import render_plan

        try:
            bsp = Bsp.read(args.map)
        except BspError as exc:
            print(exc)
            return EXIT_UNREADABLE
        plan = render_plan(bsp, size=args.size, wads=map_wads(args.map, bsp, args.game),
                           spawns=not args.no_spawns, zmin=args.zmin, zmax=args.zmax)
        plan.save(args.out)
        print(f"{args.out} ({plan.image.shape[1]}x{plan.image.shape[0]}, "
              f"{1 / plan.scale:.1f} units a pixel)" + "".join(f"; {n}" for n in plan.notes))
        return EXIT_CLEAN


__all__ = ["MapCommand"]
