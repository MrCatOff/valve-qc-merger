"""CLI wiring for ``map``: check GoldSource maps before they go on a server.

    valve-qc-merger map check   MAP.bsp ... [--game DIR] [--json OUT] [--profile zp]
    valve-qc-merger map preview MAP.bsp --out plan.png [--size 1024] [--no-spawns]
    valve-qc-merger map fix     MAP.bsp [--spawns 48] [--cso-entities remove|keep] [--ent]
                                [--out GAME_DIR] [--source DIR] [--markers NAME:COUNT]
                                [--dry-run]
    valve-qc-merger map textures MAP.bsp list [--missing|--big|--unused]
    valve-qc-merger map textures MAP.bsp replace NAME IMAGE
    valve-qc-merger map textures MAP.bsp embed [NAME ...] [--missing-only]
    valve-qc-merger map textures MAP.bsp extract OUT.wad [--strip]
    valve-qc-merger map textures MAP.bsp prune-wads
    valve-qc-merger map shared-wad MAP.bsp ... --out pack.wad
    valve-qc-merger map props MAP.bsp ... [--out GAME_DIR] [--name NAME] [--shared NAME]
                              [--studiomdl PATH] [--gc] [--dry-run]

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
        fix = actions.add_parser("fix", help="fix a map without recompiling it: entities, "
                                 "spawns, .res (the CRC stays)",
                                 description="fix a map without recompiling it")
        fix.add_argument("map", type=Path, metavar="MAP")
        fix.add_argument("--game", type=Path, action="append", default=[], metavar="DIR",
                         help="a game folder to look files up in (repeatable)")
        fix.add_argument("--profile", help="the rules profile (default: zp)")
        fix.add_argument("--rules", type=Path, metavar="FILE", help="a rules file (JSON)")
        fix.add_argument("--spawns", type=int, metavar="N",
                         help="spawns a team (default: the profile's; 0 adds none)")
        fix.add_argument("--spread", choices=("uniform", "near_existing"), default="uniform",
                         help="new spawns all over the map or near its own")
        fix.add_argument("--cso-entities", choices=("remove", "keep"), default="remove",
                         help="classes CS 1.6 drops: remove them (brush ones become func_wall)")
        fix.add_argument("--keep-missing", action="store_true",
                         help="keep entities whose model is nowhere (the server will not load)")
        fix.add_argument("--source", type=Path, action="append", default=[], metavar="DIR",
                         help="a folder (decompiled Nexon / CSO client) missing files are "
                              "copied from (repeatable)")
        fix.add_argument("--markers", action="append", default=[], metavar="NAME:COUNT",
                         help="info_target markers for a plugin, e.g. zp_supply:24")
        fix.add_argument("--keep-wads", action="store_true",
                         help="leave worldspawn's wad list as it is")
        fix.add_argument("--ent", action="store_true",
                         help="write maps/<map>.ent (ReHLDS sv_use_entity_file 1), not the BSP")
        fix.add_argument("--out", type=Path, metavar="GAME_DIR",
                         help="write into this game folder instead of the map's own")
        fix.add_argument("--mapcycle", type=Path, help="point this mapcycle at a renamed map")
        fix.add_argument("--no-res", action="store_true", help="do not write maps/<map>.res")
        fix.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")

        tex = actions.add_parser("textures", help="list, replace, embed or extract a map's "
                                 "textures", description="a map's textures")
        tex.add_argument("map", type=Path, metavar="MAP")
        tex.add_argument("op", choices=("list", "replace", "embed", "extract", "prune-wads"))
        tex.add_argument("names", nargs="*", metavar="ARG",
                         help="replace: NAME IMAGE; embed: texture names (default all); "
                              "extract: OUT.wad")
        tex.add_argument("--game", type=Path, action="append", default=[], metavar="DIR",
                         help="a game folder holding the map's WADs (repeatable)")
        filters = tex.add_mutually_exclusive_group()
        filters.add_argument("--missing", action="store_true", help="list: not found")
        filters.add_argument("--big", action="store_true", help="list: a side over 256")
        filters.add_argument("--unused", action="store_true", help="list: no face draws it")
        tex.add_argument("--missing-only", action="store_true",
                         help="embed: only textures not in Half-Life's own WADs")
        tex.add_argument("--strip", action="store_true",
                         help="extract: leave the map only the names (it lists the WAD)")
        tex.add_argument("--out", type=Path, metavar="GAME_DIR",
                         help="write into this game folder instead of the map's own")
        tex.add_argument("--mapcycle", type=Path, help="point this mapcycle at a renamed map")
        tex.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
        shared = actions.add_parser("shared-wad", help="one WAD with every texture a pack of "
                                    "maps draws", description="one WAD for a pack of maps")
        shared.add_argument("maps", nargs="+", type=Path, metavar="MAP")
        shared.add_argument("--out", type=Path, required=True, help="the WAD to write")
        shared.add_argument("--game", type=Path, action="append", default=[], metavar="DIR")

        props = actions.add_parser("props", help="merge a map's prop models into one model "
                                   "(merge-props): fewer model slots, the CRC stays",
                                   description="merge a map's prop models into one model")
        props.add_argument("maps", nargs="+", type=Path, metavar="MAP")
        props.add_argument("--out", type=Path, metavar="GAME_DIR",
                           help="the game folder the merged models and maps go to "
                                "(default: the map's own)")
        props.add_argument("--name", help="the merged model's name (default <map>_props)")
        props.add_argument("--shared", metavar="NAME",
                           help="one set of merged models for all the maps given")
        props.add_argument("--studiomdl", type=Path, help="studiomdl to compile with "
                           "(default: VQM_STUDIOMDL, PATH, the bundled one)")
        props.add_argument("--game", type=Path, action="append", default=[], metavar="DIR",
                           help="a game folder to find the models in (repeatable)")
        props.add_argument("--keep-work", type=Path, metavar="DIR",
                           help="keep the decompiled and merged sources here")
        props.add_argument("--gc", action="store_true",
                           help="delete merged parts no map names any more")
        props.add_argument("--dry-run", action="store_true",
                           help="list what would merge, write nothing")

    def run(self, args: argparse.Namespace) -> int:
        if args.action == "check":
            return self._check(args)
        if args.action == "fix":
            return self._fix(args)
        if args.action == "textures":
            return self._textures(args)
        if args.action == "shared-wad":
            return self._shared_wad(args)
        if args.action == "props":
            return self._props(args)
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


    def _fix(self, args: argparse.Namespace) -> int:
        from valve_qc_merger.mapkit.analyze import check_map, game_folder
        from valve_qc_merger.mapkit.bsp import BspError
        from valve_qc_merger.mapkit.fix import FixOptions, plan_fixes, save_plan
        from valve_qc_merger.mapkit.rules import Rules
        from valve_qc_merger.mapkit.spawns import SpawnOptions

        rules = Rules.load(args.rules, args.profile)
        try:
            report = check_map(args.map, args.game, rules)
        except (BspError, OSError) as exc:
            print(exc)
            return EXIT_UNREADABLE
        markers = []
        for text in args.markers:
            name, _, count = text.partition(":")
            if not name or not count.isdigit():
                print(f"--markers {text}: NAME:COUNT")
                return EXIT_UNREADABLE
            markers.append((name, int(count)))
        options = FixOptions(spawns=args.spawns, spawn_options=SpawnOptions(spread=args.spread),
                             cso_entities=args.cso_entities,
                             remove_missing=not args.keep_missing, sources=args.source,
                             markers=markers, wads=not args.keep_wads)
        plan = plan_fixes(report, options, rules)
        print(f"{report.map}: {len(plan.active)} operations, "
              + ("the CRC stays" if plan.crc_safe else "the CRC changes")
              + (" -> a new map name" if plan.needs_new_name else ""))
        counts: dict[str, int] = {}
        for op in plan.active:
            counts[op.code] = counts.get(op.code, 0) + 1
        for op in plan.active:
            if op.code != "spawn.generate":
                print(f"  {op.code:<16} {op.text}")
        if counts.get("spawn.generate"):
            print(f"  spawn.generate   {counts['spawn.generate']} spawns added")
        for note in plan.notes:
            print(f"  note: {note}")
        if args.dry_run:
            return EXIT_CLEAN
        result = save_plan(plan, out_dir=args.out, ent=args.ent, res=not args.no_res,
                           folders=args.game, mapcycle=args.mapcycle)
        for path in result.written:
            print(f"  wrote {path}")
        for note in result.notes:
            print(f"  note: {note}")
        print(f"CRC {result.crc_before} -> {result.crc_after}"
              + (f", saved as {result.map_name}" if result.new_name else ""))
        if args.ent:
            return EXIT_CLEAN
        written = (args.out or game_folder(args.map)) / "maps" / f"{result.map_name}.bsp"
        after = check_map(written, [game_folder(args.map), *args.game], rules)
        left = after.of_level("critical")
        print(f"after: {after.level.upper()} — spawns CT {after.spawns['ct']} / "
              f"T {after.spawns['t']}, {len(left)} critical")
        for issue in left:
            print(f"  critical {issue.code:<18} {issue.text}")
        return EXIT_CRITICAL if left else EXIT_CLEAN

    def _textures(self, args: argparse.Namespace) -> int:
        from valve_qc_merger.mapkit import fix as F
        from valve_qc_merger.mapkit.analyze import map_wads
        from valve_qc_merger.mapkit.bsp import Bsp, BspError
        from valve_qc_merger.mapkit.textures import texture_uses

        try:
            bsp = Bsp.read(args.map)
        except BspError as exc:
            print(exc)
            return EXIT_UNREADABLE
        if args.op == "list":
            wads = {w.path.name: w for w in map_wads(args.map, bsp, args.game)}
            uses = texture_uses(bsp, wads)
            if args.missing:
                uses = [u for u in uses if not u.found]
            elif args.big:
                uses = [u for u in uses if max(u.width, u.height) > 256]
            elif args.unused:
                uses = [u for u in uses if not u.faces]
            for u in uses:
                source = "embedded" if u.embedded else (u.wad or "MISSING")
                print(f"{u.name:<16} {u.width:>4}x{u.height:<4} {source:<22} {u.faces:>5} faces"
                      + ("  special" if u.special else ""))
            print(f"{len(uses)} textures")
            return EXIT_CLEAN
        try:
            if args.op == "replace":
                if len(args.names) != 2:
                    print("replace NAME IMAGE")
                    return EXIT_UNREADABLE
                plan = F.plan_replace_texture(args.map, args.names[0], Path(args.names[1]),
                                              args.game)
            elif args.op == "embed":
                plan = F.plan_embed(args.map, args.names or None, args.missing_only, args.game)
            elif args.op == "extract":
                if len(args.names) != 1:
                    print("extract OUT.wad")
                    return EXIT_UNREADABLE
                plan = F.plan_extract(args.map, args.names[0], args.strip)
            else:
                from valve_qc_merger.mapkit.analyze import check_map
                plan = F.plan_fixes(check_map(args.map, args.game),
                                    F.FixOptions(spawns=0, cso_entities="keep",
                                                 remove_missing=False))
                plan.operations = [op for op in plan.operations if op.code == "worldspawn.wad"]
        except (KeyError, ValueError, OSError) as exc:
            print(exc.args[0] if exc.args else exc)
            return EXIT_UNREADABLE
        for op in plan.active:
            print(f"  {op.code:<18} {op.text}")
        for note in plan.notes:
            print(f"  note: {note}")
        print("the CRC stays" if plan.crc_safe else "the CRC changes",
              "-> a new map name" if plan.needs_new_name else "")
        if args.dry_run or not plan.active:
            return EXIT_CLEAN
        result = F.save_plan(plan, out_dir=args.out, folders=args.game, mapcycle=args.mapcycle)
        for path in result.written:
            print(f"  wrote {path}")
        for note in result.notes:
            print(f"  note: {note}")
        print(f"CRC {result.crc_before} -> {result.crc_after}"
              + (f", saved as {result.map_name}" if result.new_name else ""))
        return EXIT_CLEAN

    def _shared_wad(self, args: argparse.Namespace) -> int:
        from valve_qc_merger.mapkit.analyze import map_wads
        from valve_qc_merger.mapkit.bsp import Bsp, BspError
        from valve_qc_merger.mapkit.textures import shared_wad, wad_of_lumps

        maps = []
        for path in _maps(args.maps):
            try:
                bsp = Bsp.read(path)
            except BspError as exc:
                print(exc)
                return EXIT_UNREADABLE
            maps.append((bsp, map_wads(path, bsp, args.game)))
        result = shared_wad(maps)
        args.out.write_bytes(wad_of_lumps(result.lumps))
        print(f"{args.out}: {len(result.textures)} textures for {len(maps)} maps "
              f"({args.out.stat().st_size / 1e6:.1f} MB)")
        for name in result.conflicts:
            print(f"  conflict: {name} differs between {', '.join(result.used_by[name])}")
        for item in result.missing:
            print(f"  missing: {item}")
        return EXIT_WARNINGS if result.conflicts or result.missing else EXIT_CLEAN

    def _props(self, args: argparse.Namespace) -> int:
        from valve_qc_merger.mapkit.analyze import game_folder
        from valve_qc_merger.mapkit.fix import save_plan
        from valve_qc_merger.mapkit.props import (
            collect,
            find_studiomdl,
            merge_props,
            stale_parts,
        )

        maps = _maps(args.maps)
        if not maps:
            print("no .bsp files found")
            return EXIT_UNREADABLE
        groups = [maps] if args.shared or len(maps) == 1 else [[m] for m in maps]
        code = EXIT_CLEAN
        for group in groups:
            name = args.shared or args.name or f"{group[0].stem}_props"
            folder = args.shared or group[0].stem
            out = args.out or game_folder(group[0])
            if args.dry_run:
                for prop in collect(group, args.game):
                    print(f"  {prop.path:<40} {len(prop.uses):>4} entities  "
                          + (f"apart: {prop.reason}" if prop.reason else "merge"))
                continue
            studiomdl = find_studiomdl(args.studiomdl)
            if studiomdl is None:
                print("studiomdl not found: pass --studiomdl (or set VQM_STUDIOMDL)")
                return EXIT_UNREADABLE
            result = merge_props(group, out, name, folders=args.game, studiomdl=studiomdl,
                                 folder=folder, keep_work=args.keep_work)
            for line in result.log:
                if line.startswith(("error", "studiomdl failed")) or "left as it was" in line:
                    print(f"  {line}")
            for prop in result.apart:
                print(f"  apart: {prop.path} — {prop.reason}")
            print(f"{', '.join(m.stem for m in group)}: {len(result.merged)} models -> "
                  f"{len(result.parts)} merged ({', '.join(result.parts) or 'none'}); "
                  f"model slots {result.slots_before} -> {result.slots_after}")
            if not result.parts:
                code = max(code, EXIT_WARNINGS)
                continue
            for plan in result.plans:
                if not plan.active:
                    continue
                saved = save_plan(plan, out_dir=args.out, folders=args.game)
                print(f"  {saved.map_name}: {len(plan.active)} entity changes, CRC "
                      f"{saved.crc_before} -> {saved.crc_after}")
            if args.gc:
                for path in stale_parts(out, folder, name):
                    path.unlink()
                    print(f"  deleted {path} (no map names it)")
        return code


__all__ = ["MapCommand"]
