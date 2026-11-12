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
    valve-qc-merger map import MAPNAME ... --source DIR [--source DIR] --out GAME_DIR
                               [--game DIR] [--overwrite]
    valve-qc-merger map build  MAP ... [--profile zp] [--source DIR] [--out GAME_DIR]
                               [--game DIR] [--no-props] [--json OUT]
    valve-qc-merger map tools [--set TOOL PATH] [--prefix TOOL CMD] [--args TOOL ARG...]
    valve-qc-merger map decompile MAP.bsp --out MAP.map
    valve-qc-merger map compile MAP.map --into GAME_DIR [--name NAME] [--steps hlcsg,...]
    valve-qc-merger map edit MAP.map
    valve-qc-merger map shrink MAP.bsp [--size 256] [--texture NAME ...] [--game DIR]
    valve-qc-merger map bspguy MAP.bsp [COMMAND ARGS...]
    valve-qc-merger map extents MAP.bsp [--subdivide 240]

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
        preview.add_argument("--issues", action="store_true",
                             help="mark the entities the check finds a problem with (yellow)")
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
        fix.add_argument("--preview", type=Path, metavar="PNG",
                         help="draw the plan from above: the map's spawns grey, new ones in "
                              "colour")

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
        props.add_argument("--groups", type=Path, metavar="JSON",
                           help="props_groups.json: {\"vehicles\": [\"models/car_red.mdl\", "
                                "…]} — a group goes into one part side by side (default: "
                                "maps/<map>_props_groups.json when there is one)")
        props.add_argument("--no-variants", action="store_true",
                           help="keep look-alikes (same geometry, other textures) as bodies of "
                                "their own instead of skins of one")
        props.add_argument("--compare", action="store_true",
                           help="merge both ways (a model set a map, one for all) and compare "
                                "slots and download size; writes nothing")

        imp = actions.add_parser("import", help="copy a map with every file it needs from "
                                 "unpacked CSO / Nexon folders into a game folder",
                                 description="import maps with their files")
        imp.add_argument("maps", nargs="+", metavar="MAPNAME",
                         help="map names in the source (or .bsp files)")
        imp.add_argument("--source", type=Path, action="append", required=True, metavar="DIR",
                         help="an unpacked client / pack folder (repeatable, first wins)")
        imp.add_argument("--out", type=Path, required=True, metavar="GAME_DIR")
        imp.add_argument("--game", type=Path, action="append", default=[], metavar="DIR",
                         help="the base game: its files are not copied (repeatable)")
        imp.add_argument("--overwrite", action="store_true",
                         help="replace files the game folder has with other content")
        imp.add_argument("--shrink-sky", action="store_true",
                         help="make sky sides over 256 smaller (256 at most)")
        build = actions.add_parser("build", help="the whole way: import, check, fix, props, "
                                   ".res, check", description="build maps for a server")
        build.add_argument("maps", nargs="+", metavar="MAP",
                           help="BSP files / folders, or map names with --source")
        build.add_argument("--profile", help="the rules profile (default: zp)")
        build.add_argument("--rules", type=Path, metavar="FILE", help="a rules file (JSON)")
        build.add_argument("--source", type=Path, action="append", default=[], metavar="DIR",
                           help="unpacked CSO / Nexon folders to import from (repeatable)")
        build.add_argument("--out", type=Path, metavar="GAME_DIR",
                           help="the game folder to build into (default: each map's own)")
        build.add_argument("--game", type=Path, action="append", default=[], metavar="DIR",
                           help="the base game (repeatable)")
        build.add_argument("--studiomdl", type=Path, help="studiomdl for the props")
        build.add_argument("--no-props", action="store_true", help="do not merge props")
        build.add_argument("--overwrite", action="store_true")
        build.add_argument("--shrink-sky", action="store_true",
                           help="import: sky sides over 256 made smaller")
        build.add_argument("--json", type=Path, metavar="OUT", help="write the reports as JSON")

        tools = actions.add_parser("tools", help="the external map tools (compilers, bspguy, a "
                                   "decompiler, TrenchBroom): status and setup",
                                   description="external map tools")
        tools.add_argument("--set", nargs=2, metavar=("TOOL", "PATH"), help="a tool's path")
        tools.add_argument("--prefix", nargs=2, metavar=("TOOL", "CMD"),
                           help="run a tool through CMD (wine, a CrossOver path)")
        tools.add_argument("--args", nargs="+", metavar=("TOOL", "ARG"),
                           help="a tool's extra arguments (the decompiler's template: {bsp} "
                                "{map})")
        tools.add_argument("--help-of", metavar="TOOL", help="print a tool's own options")
        dec = actions.add_parser("decompile", help="BSP -> MAP with the configured decompiler",
                                 description="BSP -> MAP with the configured decompiler")
        dec.add_argument("map", type=Path, metavar="MAP")
        dec.add_argument("--out", type=Path, required=True, help="the .map to write")
        comp = actions.add_parser("compile", help=".map -> .bsp with the compilers, checked, "
                                  "into maps/", description="compile a .map")
        comp.add_argument("map", type=Path, metavar="MAP")
        comp.add_argument("--into", type=Path, required=True, metavar="GAME_DIR")
        comp.add_argument("--name", help="the map name (default the .map's; a taken name gets _sp)")
        comp.add_argument("--steps", default=",".join(("hlcsg", "hlbsp", "hlvis", "hlrad")),
                          help="compilers to run, comma separated")
        comp.add_argument("--wad", type=Path, action="append", default=[],
                          help="WADs the compilers need (repeatable)")
        comp.add_argument("--game", type=Path, action="append", default=[], metavar="DIR")
        edit = actions.add_parser("edit", help="open a .map in TrenchBroom",
                                  description="open a .map in TrenchBroom")
        edit.add_argument("map", type=Path, metavar="MAP")
        shrink = actions.add_parser("shrink", help="make textures over a size smaller (decompile, "
                                    "rescale, recompile)", description="shrink big textures")
        shrink.add_argument("map", type=Path, metavar="MAP")
        shrink.add_argument("--size", type=int, default=256)
        shrink.add_argument("--texture", action="append", default=[], metavar="NAME")
        shrink.add_argument("--game", type=Path, action="append", default=[], metavar="DIR")
        guy = actions.add_parser("bspguy", help="run a bspguy command on a copy of a map "
                                 "(merge, noclip, simplify, delete, transform … — what your "
                                 "bspguy lists); the result is checked and saved as <map>_sp",
                                 description="run bspguy on a copy of a map")
        guy.add_argument("map", type=Path, metavar="MAP")
        guy.add_argument("command", nargs="?", help="the bspguy command (none: list them)")
        guy.add_argument("args", nargs=argparse.REMAINDER, help="its arguments")
        guy.add_argument("--game", type=Path, action="append", default=[], metavar="DIR")
        ext = actions.add_parser("extents", help="Bad surface extents: decompile and compile "
                                 "again with -subdivide", description="recompile subdivided")
        ext.add_argument("map", type=Path, metavar="MAP")
        ext.add_argument("--subdivide", type=int, default=240)
        ext.add_argument("--game", type=Path, action="append", default=[], metavar="DIR")

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
        if args.action == "import":
            return self._import(args)
        if args.action == "build":
            return self._build(args)
        if args.action in ("tools", "decompile", "compile", "edit", "shrink", "bspguy",
                           "extents"):
            return self._external(args)
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
        marks = None
        if args.issues:
            from valve_qc_merger.mapkit.analyze import check_map, marked_entities
            marks = marked_entities(check_map(args.map, args.game))
        plan = render_plan(bsp, size=args.size, wads=map_wads(args.map, bsp, args.game),
                           spawns=not args.no_spawns, zmin=args.zmin, zmax=args.zmax,
                           marks=marks)
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
        if args.preview:
            from valve_qc_merger.mapkit.analyze import map_wads
            from valve_qc_merger.mapkit.bsp import Bsp
            from valve_qc_merger.mapkit.render import render_plan
            from valve_qc_merger.server.entities import parse
            bsp = Bsp.read(args.map)
            before = parse(bsp.entity_text)
            after = plan.apply(before)
            kept = len([op for op in plan.active if op.code in ("spawn.generate", "entity.add")])
            render_plan(bsp, wads=map_wads(args.map, bsp, args.game), entities=after,
                        grey_spawns_before=len(after) - kept).save(args.preview)
            print(f"  plan: {args.preview}")
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
                print(f"{u.name:<16} {u.width:>4}x{u.height:<4} {source:<22} {u.faces:>5} faces "
                      f"{u.area:>12,.0f} u²" + ("  special" if u.special else "")
                      + "".join(f"  {p}" for p in u.prefixes))
            print(f"{len(uses)} textures")
            return EXIT_CLEAN
        try:
            if args.op == "replace":
                if len(args.names) < 2 or len(args.names) % 2:
                    print("replace NAME IMAGE [NAME IMAGE ...] (an animated texture: +0NAME "
                          "NAME_0.png — NAME_1.png … follow)")
                    return EXIT_UNREADABLE
                pairs = [(args.names[i], Path(args.names[i + 1]))
                         for i in range(0, len(args.names), 2)]
                plan = F.plan_replace_textures(args.map, pairs, args.game)
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
        if args.compare:
            return self._props_compare(args, maps)
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
            from valve_qc_merger.mapkit.props import read_groups
            groups_file = args.groups or group[0].with_name(f"{group[0].stem}_props_groups.json")
            groups = read_groups(groups_file) if groups_file.is_file() else None
            result = merge_props(group, out, name, folders=args.game, studiomdl=studiomdl,
                                 folder=folder, keep_work=args.keep_work, groups=groups,
                                 variants=not args.no_variants)
            for line in result.log:
                if line.startswith(("error", "studiomdl failed")) or "left as it was" in line:
                    print(f"  {line}")
            for prop in result.apart:
                print(f"  apart: {prop.path} — {prop.reason}")
            for prop in result.merged:
                if prop.variant_of:
                    print(f"  {prop.path}: " + (f"skin {prop.skin} of {prop.variant_of}"
                                                if prop.skin else f"the same as {prop.variant_of}"))
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
            from valve_qc_merger.mapkit.props import unused_models
            spare = unused_models(result, out)
            if spare:
                more = f" (+{len(spare) - 6})" if len(spare) > 6 else ""
                print(f"  {len(spare)} original models no other map of {out / 'maps'} uses "
                      f"(they can go): {', '.join(spare[:6])}{more}")
            if args.gc:
                for path in stale_parts(out, folder, name):
                    path.unlink()
                    print(f"  deleted {path} (no map names it)")
        return code

    def _import(self, args: argparse.Namespace) -> int:
        from valve_qc_merger.mapkit.analyze import check_map
        from valve_qc_merger.mapkit.nexon import (
            ImportResult,
            SourceIndex,
            import_map,
            write_record,
        )

        index = SourceIndex.build(args.source)
        print(f"source: {len(index.files)} files")
        result = ImportResult()
        for name in args.maps:
            import_map(name, index, args.out, base=args.game, overwrite=args.overwrite,
                       result=result, shrink_sky=args.shrink_sky)
        states: dict[str, int] = {}
        for record in result.files.values():
            states[record.state] = states.get(record.state, 0) + 1
        print(", ".join(f"{v} {k}" for k, v in sorted(states.items())))
        for record in result.files.values():
            if record.conversions:
                print(f"  converted {record.path}: {'; '.join(record.conversions)}")
            if record.state in ("missing", "renamed", "kept"):
                print(f"  {record.state:<8} {record.path}")
        for note in result.notes:
            print(f"  note: {note}")
        print(f"record: {write_record(result, args.out)}")
        code = EXIT_CLEAN if result.maps else EXIT_UNREADABLE
        for name in result.maps:
            report = check_map(args.out / "maps" / f"{name}.bsp", args.game)
            print(f"{name}: {report.level.upper()} after import")
            for issue in report.of_level("critical"):
                print(f"  critical {issue.code:<18} {issue.text}")
            if report.level == "critical":
                code = max(code, EXIT_CRITICAL)
        return code

    def _build(self, args: argparse.Namespace) -> int:
        from valve_qc_merger.mapkit.rules import Rules
        from valve_qc_merger.mapkit.workflow import BuildOptions, build_maps, write_report

        rules = Rules.load(args.rules, args.profile)
        items = [str(p) for p in _maps([Path(m) for m in args.maps if Path(m).exists()])]
        items += [m for m in args.maps if not Path(m).exists()]
        options = BuildOptions(out=args.out, sources=args.source, folders=args.game,
                               studiomdl=args.studiomdl,
                               props=False if args.no_props else None,
                               overwrite=args.overwrite, shrink_sky=args.shrink_sky)
        results = build_maps(items, rules, options)
        code = EXIT_CLEAN
        for build in results:
            for step in build.steps:
                print(f"  {build.name}: {step}")
            if build.error:
                code = max(code, EXIT_UNREADABLE)
                continue
            if build.after is not None:
                left = build.after.of_level("critical")
                for issue in left:
                    print(f"  {build.name}: critical {issue.code} {issue.text}")
                code = max(code, EXIT_CRITICAL if left else
                           EXIT_WARNINGS if build.after.level == "warning" else EXIT_CLEAN)
        if args.json:
            print(f"report: {write_report(results, args.json)}")
        return code

    def _external(self, args: argparse.Namespace) -> int:
        from valve_qc_merger.mapkit import external as X
        from valve_qc_merger.mapkit.analyze import check_map, game_folder

        if args.action == "tools":
            settings = X.load_settings()
            changed = False
            if args.set:
                settings.setdefault(args.set[0], {})["path"] = args.set[1]
                changed = True
            if args.prefix:
                settings.setdefault(args.prefix[0], {})["prefix"] = args.prefix[1].split()
                changed = True
            if args.args:
                settings.setdefault(args.args[0], {})["args"] = args.args[1:]
                changed = True
            if changed:
                print(f"saved {X.save_settings(settings)}")
            if args.help_of:
                tool = X.find_tool(args.help_of, settings)
                text = X.tool_help(tool) if tool.available else ""
                print(text or f"{args.help_of}: no usage text ({tool.hint})")
                return EXIT_CLEAN
            for name in X.TOOLS:
                tool = X.find_tool(name, settings)
                print(f"{name:<12} {tool.path if tool.available else 'not set up'}"
                      + (f"  (via {' '.join(tool.prefix)})" if tool.prefix else "")
                      + ("" if tool.available else f" — {tool.hint}"))
            return EXIT_CLEAN
        if args.action == "edit":
            tool = X.find_tool("trenchbroom")
            if not tool.available:
                print(f"TrenchBroom is not set up — {tool.hint}")
                return EXIT_UNREADABLE
            X.open_in_editor(args.map)
            return EXIT_CLEAN
        if args.action == "decompile":
            if not X.find_tool("decompiler").available:
                print(f"no decompiler set up — {X.HINTS['decompiler']}")
                return EXIT_UNREADABLE
            run = X.decompile_map(args.map, args.out)
            print("decompiled: brushes are approximate — check the map before compiling"
                  if run.ok and args.out.is_file() else f"decompiler failed (exit {run.code})")
            return EXIT_CLEAN if run.ok and args.out.is_file() else EXIT_CRITICAL
        if args.action == "compile":
            steps = tuple(s.strip() for s in args.steps.split(",") if s.strip())
            result = X.compile_map(args.map, steps=steps, wads=args.wad)
            for note in result.notes:
                print(f"  note: {note}")
            for error in result.errors:
                print(f"  error: {error}")
            if result.bsp is None:
                return EXIT_CRITICAL
            dest = X.install_map(result.bsp, args.into, args.name or args.map.stem)
            report = check_map(dest, args.game)
            print(f"{dest}: {report.level.upper()}")
            for issue in report.of_level("critical"):
                print(f"  critical {issue.code:<18} {issue.text}")
            return EXIT_CRITICAL if report.level == "critical" else EXIT_CLEAN
        if args.action == "bspguy":
            tool = X.find_tool("bspguy")
            if not tool.available:
                print(f"bspguy is not set up — {tool.hint}")
                return EXIT_UNREADABLE
            commands = X.bspguy_commands(tool)
            if not args.command:
                print("bspguy commands: " + (", ".join(commands) or "(its usage lists none)"))
                return EXIT_CLEAN
            if commands and args.command not in commands:
                print(f"this bspguy has no '{args.command}' (it has: {', '.join(commands)})")
                return EXIT_UNREADABLE
            run, copy = X.run_bspguy(args.map, args.command, list(args.args))
            if not run.ok:
                print(f"bspguy failed (exit {run.code})")
                return EXIT_CRITICAL
            if args.command in ("info", "exportobj", "exportlit", "exportrad"):
                return EXIT_CLEAN
            dest = X.install_map(copy, game_folder(args.map), args.map.stem)
            report = check_map(dest, args.game)
            print(f"{dest}: {report.level.upper()}")
            return EXIT_CRITICAL if report.level == "critical" else EXIT_CLEAN
        if args.action == "extents":
            result = X.recompile_subdivided(args.map, args.subdivide, folders=args.game)
            for note in result.notes:
                print(f"  note: {note}")
            if result.bsp is None:
                return EXIT_CRITICAL
            dest = X.install_map(result.bsp, game_folder(args.map), args.map.stem)
            report = check_map(dest, args.game)
            left = [i for i in report.issues if i.code == "bad_extents"]
            print(f"{dest}: {report.level.upper()}"
                  + (f", still {left[0].text}" if left else ", no bad extents"))
            return EXIT_CRITICAL if report.level == "critical" else EXIT_CLEAN
        result = X.shrink_textures(args.map, args.size, args.texture or None,
                                   folders=args.game)
        for name, size in result.shrunk.items():
            print(f"  {name} -> {size[0]}x{size[1]}")
        for note in result.notes:
            print(f"  note: {note}")
        if result.bsp is None:
            return EXIT_CRITICAL if result.shrunk else EXIT_CLEAN
        game = game_folder(args.map)
        if result.wad is not None:
            import shutil
            shutil.copy2(result.wad, game / result.wad.name)
        dest = X.install_map(result.bsp, game, args.map.stem)
        print(f"{dest}: {check_map(dest, args.game).level.upper()} (a new map: its .res lists "
              f"{result.wad.name if result.wad else ''})")
        return EXIT_CLEAN

    def _props_compare(self, args: argparse.Namespace, maps: list[Path]) -> int:
        import tempfile

        from valve_qc_merger.mapkit.props import find_studiomdl, merge_props

        studiomdl = find_studiomdl(args.studiomdl)
        if studiomdl is None:
            print("studiomdl not found: pass --studiomdl (or set VQM_STUDIOMDL)")
            return EXIT_UNREADABLE
        with tempfile.TemporaryDirectory(prefix="vqm_compare_") as tmp:
            rows = []
            total = {"per map": 0, "shared": 0}
            for path in maps:
                one = merge_props([path], Path(tmp) / "one" / path.stem, f"{path.stem}_props",
                                  folders=args.game, studiomdl=studiomdl)
                size = sum((Path(tmp) / "one" / path.stem / p).stat().st_size
                           for p in one.parts if (Path(tmp) / "one" / path.stem / p).is_file())
                rows.append((path.stem, one.slots_before, one.slots_after, size))
                total["per map"] += size
            shared = merge_props(maps, Path(tmp) / "shared", "shared_props", folders=args.game,
                                 studiomdl=studiomdl, folder="shared")
            parts = {p: (Path(tmp) / "shared" / p).stat().st_size for p in shared.parts
                     if (Path(tmp) / "shared" / p).is_file()}
            total["shared"] = sum(parts.values())
        print(f"{'map':<20} {'models':>6} {'per map':>8} {'shared':>7}")
        merged = {m.path.lower() for m in shared.merged}
        for (name, before, after, _size), path in zip(rows, maps, strict=True):
            from valve_qc_merger.mapkit.props import collect
            own = {p.path.lower() for p in collect([path], args.game)}
            shared_slots = len(own - merged) + (len(shared.parts) if own & merged else 0)
            print(f"{name:<20} {before:>6} {after:>8} {shared_slots:>7}")
        print(f"download without a cache: per map {total['per map'] / 1e6:.1f} MB (each map "
              f"its own), shared {total['shared'] / 1e6:.1f} MB (once for every map)")
        return EXIT_CLEAN


__all__ = ["MapCommand"]
