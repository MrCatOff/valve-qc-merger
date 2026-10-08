"""A server package: everything to put on a ReHLDS server and its FastDL host.

``export_package`` writes, under an output folder:

- ``cstrike/`` — the compiled models of the chosen builds (+ ``T.mdl``) and
  their manifests where Deploy would put them, plus every sound the models'
  events play (from the library / game folder) under ``sound/``. The same
  tree is what ``sv_downloadurl`` (FastDL) serves: upload it as is;
- ``amxx/vqm_resources.inc`` — an AMXX include for a ReAPI weapon plugin:
  the paths to precache, a ``vqm_precache()`` that precaches them (client
  sounds through ``precache_generic`` on ReHLDS, or ``precache_sound``), and
  per weapon its model, ``pev_body`` and sequence numbers;
- ``vqm_resources.res`` — the same files in the ``.res`` format (copy as
  ``maps/<map>.res`` to make a map send them);
- ``rechecker/resources.ini`` — ReChecker rules: every packed model and
  sprite with its hash accepted, any modified copy kicked;
- ``package_report.txt`` — files, sizes and what a new player downloads;
- ``update/`` — from the second export on: only the new and changed files
  (changed models and sprites get new names, see :mod:`.versions`) and
  ``removed.txt``; ``vqm_package.json`` remembers the export.
"""

from __future__ import annotations

import configparser
import json
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from valve_qc_merger.project.model import Project

MOD_FOLDER = "cstrike"
PLUGIN_TEMPLATE = "vqm_weapons.sma"  # storage/server: the ReAPI plugin template


@dataclass
class PackageResult:
    root: Path
    files: dict[str, int] = field(default_factory=dict)  # mod-relative path -> bytes
    models: list[str] = field(default_factory=list)  # precache_model paths
    client_sounds: list[str] = field(default_factory=list)  # relative to sound/
    # of those, the ones others hear (shots, swings): precache_sound, the rest generic
    server_sounds: list[str] = field(default_factory=list)
    weapons: dict[str, dict[str, object]] = field(default_factory=dict)
    sprites: list[str] = field(default_factory=list)  # effect sprites (precache_model)
    hud_files: list[str] = field(default_factory=list)  # HUD txt + sheets (generic)
    missing_sounds: list[str] = field(default_factory=list)
    skipped_builds: list[str] = field(default_factory=list)  # not run / not compiled
    versions: object = None  # a versions.VersionReport

    @property
    def total(self) -> int:
        return sum(self.files.values())


def _manifest(path: Path) -> dict[str, dict[str, object]]:
    """A build manifest (ini / json / toml) as {weapon: {key: value}}."""
    if path.suffix.lower() == ".ini":
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str  # keep anim_* case
        parser.read(path, encoding="utf-8")
        return {s: dict(parser.items(s)) for s in parser.sections()}
    text = path.read_text(encoding="utf-8")
    data = json.loads(text) if path.suffix.lower() == ".json" else tomllib.loads(text)
    if isinstance(data, dict) and "models" in data and isinstance(data["models"], dict):
        data = data["models"]
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) \
        else {}


def export_package(project: Project, out: Path, builds: list[str] | None = None) -> PackageResult:
    from valve_qc_merger.project import sounds as library
    from valve_qc_merger.project.model import MANIFEST_SUFFIXES, ProjectError
    from valve_qc_merger.server import versions
    from valve_qc_merger.server.budget import _CLIENT_SOUND
    out = Path(out)
    mod = out / MOD_FOLDER
    previous = versions.load_state(out)
    if previous is not None and mod.is_dir():
        shutil.rmtree(mod)  # our own tree from the last export: rebuilt from scratch
    result = PackageResult(out)
    from valve_qc_merger.project.sounds import project_sound_kinds
    names = builds if builds is not None else sorted(project.builds)
    texts: list[str] = []
    for name in names:
        try:
            pairs = project.deploy_files(name, root=mod)
        except ProjectError:
            result.skipped_builds.append(name)
            continue
        for source, destination in pairs:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            relative = destination.relative_to(mod).as_posix()
            result.files[relative] = destination.stat().st_size
            if relative.lower().endswith(".mdl") and not re.search(
                    r"(?i)(t|\d\d)\.mdl$", relative):
                result.models.append(relative)
            elif source.suffix.lower() in MANIFEST_SUFFIXES:
                for weapon, entry in _manifest(source).items():
                    model = str(entry.get("model", ""))
                    folder = Path(relative).parent.as_posix()
                    result.weapons[weapon] = {**entry, "model": f"{folder}/{model}"
                                              if model and "/" not in model else model}
        for qc in (project.build_dir(name) / "output").rglob("*.qc"):
            text = qc.read_text(encoding="latin-1")
            texts.append(text)
            for sound in _CLIENT_SOUND.findall(text):
                result.client_sounds.append(sound.replace("\\", "/"))
    kinds = project_sound_kinds(project, texts)
    # stock sounds (the game precaches them) and sounds aliased away are not ours
    by_key = {s.lower(): s for s in result.client_sounds}
    result.client_sounds = sorted({by_key.get(k, k) for k in kinds}, key=str.lower)
    result.server_sounds = [s for s in result.client_sounds
                            if kinds.get(s.lower()) == "sound"]
    for sound in result.client_sounds:
        source = library.resolve(project, sound)
        if source is None:
            result.missing_sounds.append(sound)
            continue
        destination = mod / "sound" / sound
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        result.files[f"sound/{sound}"] = destination.stat().st_size
    result.models.sort(key=str.lower)
    from valve_qc_merger.project import sprites as sprite_library
    hud = {h.lower() for h in sprite_library.hud_files(project)}
    for name in sprite_library.list_sprites(project):
        destination = mod / "sprites" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sprite_library.sprite_path(project, name), destination)
        game_path = f"sprites/{name}"
        result.files[game_path] = destination.stat().st_size
        if game_path.lower() in hud:
            result.hud_files.append(game_path)
        elif name.lower().endswith(".spr"):
            result.sprites.append(game_path)
    published, result.versions, state = versions.apply_versions(
        mod, result.files, previous, set(result.models) | set(result.sprites))
    _publish(result, mod, published, MANIFEST_SUFFIXES)
    versions.write_update(out, mod, result.versions)
    (out / versions.STATE_FILE).write_text(json.dumps(state, indent=1), encoding="utf-8")
    (out / "amxx").mkdir(parents=True, exist_ok=True)
    (out / "amxx" / "vqm_resources.inc").write_text(
        amxx_include(result, project.settings.client_sounds, project.name), encoding="utf-8")
    from valve_qc_merger.resources import data_root
    template = data_root() / "storage" / "server" / PLUGIN_TEMPLATE
    if template.is_file():
        shutil.copyfile(template, out / "amxx" / PLUGIN_TEMPLATE)
        (out / "amxx" / "vqm_weapons.ini").write_text(plugin_config(result), encoding="utf-8")
    (out / "vqm_resources.res").write_text(res_file(result), encoding="utf-8")
    from valve_qc_merger.server.rechecker import rules
    (out / "rechecker").mkdir(parents=True, exist_ok=True)
    (out / "rechecker" / "resources.ini").write_text(
        rules({path: mod / path for path in result.files}), encoding="utf-8")
    (out / "package_report.txt").write_text(report(result), encoding="utf-8")
    return result


def _publish(result: PackageResult, mod: Path, published: dict[str, str],
             manifest_suffixes: tuple[str, ...] | frozenset[str]) -> None:
    """Point the result at the published (versioned) names; rewrite the
    model names inside the packed build manifests."""
    result.files = {published.get(p, p): (mod / published.get(p, p)).stat().st_size
                    for p in result.files}
    result.models = sorted((published.get(m, m) for m in result.models), key=str.lower)
    result.sprites = [published.get(s, s) for s in result.sprites]
    result.hud_files = [published.get(h, h) for h in result.hud_files]
    renamed = {Path(a).name: Path(b).name for a, b in published.items() if a != b}
    for entry in result.weapons.values():
        model = str(entry.get("model", ""))
        entry["model"] = published.get(model, model)
    if not renamed:
        return
    for path in result.files:
        if Path(path).suffix.lower() in manifest_suffixes:
            text = (mod / path).read_text(encoding="utf-8")
            for old, new in renamed.items():
                text = re.sub(rf"(?<![\w.]){re.escape(old)}(?![\w])", new, text)
            (mod / path).write_text(text, encoding="utf-8")


def _ident(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper() or "X"


def amxx_include(result: PackageResult, client_sounds: str = "generic",
                 pack: str = "pack") -> str:
    guard = f"_vqm_{_ident(pack).lower()}_included"
    server = {s.lower() for s in result.server_sounds}
    if client_sounds == "sound":
        server = {s.lower() for s in result.client_sounds}
    generic_list = [s for s in result.client_sounds if s.lower() not in server]
    server_list = [s for s in result.client_sounds if s.lower() in server]
    lines = [
        "// Generated by valve-qc-merger Studio — re-export instead of editing.",
        "// For a ReHLDS + ReGameDLL + ReAPI weapon plugin:",
        "//   public plugin_precache() { vqm_precache(); }",
        "// then use VQM_<WEAPON>_MODEL / _BODY / _ANIM_* for each merged weapon.",
        f"#if defined {guard}", "  #endinput", "#endif", f"#define {guard}", "",
        "#include <amxmodx>", "",
        "// precache_model: the compiled (merged) models",
        "stock const VQM_MODELS[][] = {",
        *[f'\t"{m}",' for m in result.models or ["models/null.mdl"]],
        "};",
        f"stock const VQM_MODEL_COUNT = {len(result.models)};", "",
        "// view-model sounds only the shooter hears (event 5004): precache_generic —",
        "// ReHLDS gives it 4096 slots and the client plays them by file name",
        "stock const VQM_CLIENT_SOUNDS[][] = {",
        *[f'\t"sound/{s}",' for s in generic_list or ["-"]],
        "};",
        f"stock const VQM_CLIENT_SOUND_COUNT = {len(generic_list)};", "",
        "// view-model sounds others hear too (shots, swings — your plugin plays them",
        "// from the server, emit_sound): precache_sound, paths relative to sound/",
        "stock const VQM_SERVER_SOUNDS[][] = {",
        *[f'\t"{s}",' for s in server_list or ["-"]],
        "};",
        f"stock const VQM_SERVER_SOUND_COUNT = {len(server_list)};", "",
        "// effect sprites: precache_model (they take model slots)",
        "stock const VQM_SPRITES[][] = {",
        *[f'\t"{s}",' for s in result.sprites or ["-"]],
        "};",
        f"stock const VQM_SPRITE_COUNT = {len(result.sprites)};", "",
        "// weapon HUD files (sprites/weapon_*.txt + their sheets): download only",
        "stock const VQM_HUD_FILES[][] = {",
        *[f'\t"{h}",' for h in result.hud_files or ["-"]],
        "};",
        f"stock const VQM_HUD_FILE_COUNT = {len(result.hud_files)};", "",
        "stock vqm_precache()", "{",
        "\tfor (new i = 0; i < VQM_MODEL_COUNT; i++) precache_model(VQM_MODELS[i]);",
        "\tfor (new i = 0; i < VQM_SPRITE_COUNT; i++) precache_model(VQM_SPRITES[i]);",
        "\tfor (new i = 0; i < VQM_HUD_FILE_COUNT; i++) precache_generic(VQM_HUD_FILES[i]);",
        "\tfor (new i = 0; i < VQM_CLIENT_SOUND_COUNT; i++) "
        "precache_generic(VQM_CLIENT_SOUNDS[i]);",
        "\tfor (new i = 0; i < VQM_SERVER_SOUND_COUNT; i++) "
        "precache_sound(VQM_SERVER_SOUNDS[i]);",
        "}", "",
        "// merged weapons: the model to set and the body value that selects the weapon",
    ]
    for weapon, entry in sorted(result.weapons.items()):
        ident = _ident(weapon)
        lines.append(f'#define VQM_{ident}_MODEL "{entry.get("model", "")}"')
        if "pev_body" in entry:
            lines.append(f"#define VQM_{ident}_BODY {int(entry['pev_body'])}")
        if "skin" in entry:
            lines.append(f"#define VQM_{ident}_SKIN {int(entry['skin'])}")
        seen: set[str] = set()
        for key, value in entry.items():
            if key.startswith("anim_"):
                anim = _ident(key[5:])
                if anim in seen:
                    continue
                seen.add(anim)
                try:
                    lines.append(f"#define VQM_{ident}_ANIM_{anim} {int(value)}")
                except (TypeError, ValueError):
                    continue
    lines += _runtime_tables(result)
    return "\n".join(lines) + "\n"


_SHOOT = re.compile(r"(?i)^anim_(shoot|fire)(?!.*empty)")


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


def _runtime_tables(result: PackageResult) -> list[str]:
    """Every merged weapon as rows a plugin looks up by manifest name at run
    time (vqm_weapons.sma): model, body, skin, its sequences in the source
    model's order (the stock weapon's animation numbers -> the merged
    model's) and its shoot sequences."""
    names, models, bodies, skins = [], [], [], []
    seq_first, seq_count, seqs = [], [], []
    shoot_first, shoot_count, shoots = [], [], []
    for weapon, entry in sorted(result.weapons.items()):
        anims = [(k, _int(v, -1)) for k, v in entry.items() if k.startswith("anim_")]
        anims = [(k, v) for k, v in anims if v >= 0]
        names.append(weapon)
        models.append(str(entry.get("model", "")))
        bodies.append(_int(entry.get("pev_body")))
        skins.append(_int(entry.get("skin")))
        seq_first.append(len(seqs))
        seq_count.append(len(anims))
        seqs += [v for _k, v in anims]
        picked = [v for k, v in anims if _SHOOT.match(k)]
        shoot_first.append(len(shoots))
        shoot_count.append(len(picked))
        shoots += picked

    def strings(values: list[str]) -> list[str]:
        return [f'\t"{v}",' for v in values or ["-"]]

    def numbers(values: list[int]) -> str:
        return ", ".join(map(str, values or [0]))

    return [
        "", "// the same weapons as rows, looked up by manifest name at run time",
        "// (vqm_weapons.sma): VQM_SEQ[VQM_SEQ_FIRST[i] + n] is the merged model's",
        "// sequence for the source model's n-th — the stock weapon's animation n",
        f"stock const VQM_WEAPON_COUNT = {len(names)};",
        "stock const VQM_NAMES[][] = {", *strings(names), "};",
        "stock const VQM_MODEL_PATHS[][] = {", *strings(models), "};",
        f"stock const VQM_BODIES[] = {{ {numbers(bodies)} }};",
        f"stock const VQM_SKINS[] = {{ {numbers(skins)} }};",
        f"stock const VQM_SEQ_FIRST[] = {{ {numbers(seq_first)} }};",
        f"stock const VQM_SEQ_COUNT[] = {{ {numbers(seq_count)} }};",
        f"stock const VQM_SEQ[] = {{ {numbers(seqs)} }};",
        f"stock const VQM_SHOOT_FIRST[] = {{ {numbers(shoot_first)} }};",
        f"stock const VQM_SHOOT_COUNT[] = {{ {numbers(shoot_count)} }};",
        f"stock const VQM_SHOOT[] = {{ {numbers(shoots)} }};",
        "",
        "// the row of a manifest name, or -1",
        "stock vqm_find(const name[])", "{",
        "\tfor (new i = 0; i < VQM_WEAPON_COUNT; i++)",
        "\t\tif (equali(VQM_NAMES[i], name)) return i;",
        "\treturn -1;", "}",
    ]


def plugin_config(result: PackageResult) -> str:
    """A ``vqm_weapons.ini`` to fill in: the format and every manifest name."""
    lines = ["; vqm_weapons.sma: which merged weapon each stock weapon becomes.",
             "; <stock weapon> = <v_ name> <p_ name> <w_ name> [<shot sound>]",
             '; "-" keeps the stock model; the shot sound (relative to sound/) turns on',
             "; the plugin's own shoot sequence + sound for that weapon. Example:",
             ";weapon_ak47 = v_ak47long_hands p_ak47long w_ak47long weapons/ak47long-1.wav",
             ""]
    for prefix, title in (("v_", "view models"), ("p_", "player-held"), ("w_", "world")):
        names = sorted(n for n in result.weapons if n.lower().startswith(prefix))
        if names:
            lines.append(f"; {title}: " + " ".join(names))
    return "\n".join(lines) + "\n"


def res_file(result: PackageResult) -> str:
    lines = ["// Files of the package for clients to download (.res format).",
             "// Copy as maps/<map>.res to make that map send them."]
    lines += sorted(result.files, key=str.lower)
    return "\n".join(lines) + "\n"


def _size(count: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "bytes" else f"{count:.1f} {unit}"
        count /= 1024
    return str(count)


def report(result: PackageResult) -> str:
    kinds: dict[str, int] = {}
    for path, size in result.files.items():
        kind = "sounds" if path.startswith("sound/") else "sprites" if path.startswith(
            "sprites/") else ("manifests" if not path.lower().endswith(".mdl") else "models")
        kinds[kind] = kinds.get(kind, 0) + size
    lines = [f"Server package — {len(result.files)} files, {_size(result.total)}",
             f"A new player downloads at most {_size(result.total)} (models + sounds).", "",
             *[f"  {kind:<10} {_size(size)}" for kind, size in sorted(kinds.items())], "",
             f"precache_model: {len(result.models)} models + {len(result.sprites)} sprites",
             f"precache_generic HUD files: {len(result.hud_files)}",
             f"view-model sounds: {len(result.client_sounds)} ("
             f"{len(result.server_sounds)} precache_sound — others hear them, the rest "
             "precache_generic)"]
    if result.missing_sounds:
        lines += ["", "Sounds the models play but neither the library nor the game folder "
                  "has (not packed):", *[f"  sound/{s}" for s in result.missing_sounds]]
    if result.skipped_builds:
        lines += ["", "Builds left out (not run or not compiled): "
                  + ", ".join(result.skipped_builds)]
    if result.versions is not None:
        from valve_qc_merger.server.versions import report_lines
        lines += ["", *report_lines(result.versions)]
    lines += ["", f"Upload {MOD_FOLDER}/ to the server and to the sv_downloadurl host "
              "(FastDL) as is; add amxx/vqm_resources.inc to your weapon plugin; append "
              "rechecker/resources.ini to ReChecker's resources.ini to kick modified copies."]
    return "\n".join(lines) + "\n"


__all__ = ["PackageResult", "amxx_include", "export_package", "plugin_config", "report",
           "res_file"]
