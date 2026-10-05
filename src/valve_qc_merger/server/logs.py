"""Read server (and client console) logs for the errors a resource pack causes.

Each rule matches one kind of line — engine ``Host_Error``s, precache
limits, missing or unprecached files, edict and memory exhaustion, AMXX
plugin failures — and says what it means and where in the studio to fix
it. Lines are grouped by rule and by the file or plugin they name, with a
count and the first line number, so a log of thousands of lines reads as a
short list. The patterns are deliberately loose (case-insensitive, key
words only): wording differs a little between HLDS, ReHLDS and AMXX
versions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Rule:
    key: str
    severity: str  # error | warning
    pattern: re.Pattern[str]
    title: str
    advice: str
    tab: str = ""  # the Server tab that helps: budget, doctor, unprecache, maps…


def _rule(key: str, severity: str, pattern: str, title: str, advice: str,
          tab: str = "") -> Rule:
    return Rule(key, severity, re.compile(pattern, re.IGNORECASE), title, advice, tab)


RULES: tuple[Rule, ...] = (
    _rule("model_limit", "error",
          r"precache_model\w*:(?:.*?'(?P<subject>[^']+)')?.*over the \d+ limit",
          "Model precache limit (512) reached",
          "Every model, sprite and brush model of the map counts. Merge weapons into "
          "bodygroups, unprecache stock models you replace, check the map's brush models.",
          "budget"),
    _rule("sound_limit", "error",
          r"precache_sound\w*:(?:.*?'(?P<subject>[^']+)')?.*over the \d+ limit",
          "Sound precache limit (512) reached",
          "Move view-model client sounds to precache_generic (ReHLDS: 4096 slots, the "
          "package's include does it), drop duplicate sounds.", "budget"),
    _rule("generic_limit", "error",
          r"precache_generic\w*:(?:.*?'(?P<subject>[^']+)')?.*over the \d+ limit",
          "Generic precache limit reached",
          "Stock HLDS allows 512 generic files, ReHLDS 4096: run ReHLDS, or send fewer "
          "HUD files and client sounds.", "budget"),
    _rule("not_precached_model", "error",
          r"(?:SV_ModelIndex: model|no precache:)\s*(?P<subject>\S+?\.(?:mdl|spr))",
          "A model is used without being precached",
          "A plugin sets a model it never precached (or precached under another path): "
          "precache it in plugin_precache — the package's vqm_precache() lists every "
          "model.", "budget"),
    _rule("not_precached_sound", "warning",
          r"SV_StartSound:\s*(?P<subject>\S+?\.wav)\s+not precached",
          "A sound is played without being precached",
          "A plugin or the map plays a sound nobody precached: it is silent. Precache it "
          "(or play it as a client sound through the view model).", ""),
    _rule("precache_late", "error",
          r"Precache can only be done in spawn functions",
          "Precache called after the map started",
          "A plugin precaches outside plugin_precache: move the precache_* calls there.", ""),
    _rule("missing_model", "error",
          r"Mod_NumForName:\s*(?P<subject>\S+)\s+not found",
          "A model the server needs is missing",
          "The file is not on the server (or its path differs in case — the Linux server "
          "is case-sensitive). Run the Doctor on the mod folder.", "doctor"),
    _rule("model_version", "error",
          r"(?P<subject>\S+\.mdl)?\s*has wrong version number",
          "A model has an unsupported version",
          "Not a GoldSource (v10) model: recompile it with GoldSource studiomdl.", "doctor"),
    _rule("missing_file", "warning",
          r"(?:couldn't open|can't open|could not open|S_LoadSound: Couldn't load)\s+"
          r"(?P<subject>[\w./\\-]+\.(?:wav|spr|wad|tga|bmp|txt|mdl))",
          "A file could not be opened",
          "Missing on the server (or the client never got it). Check the Doctor, the map's "
          ".res (Maps) and the FastDL mirror.", "doctor"),
    _rule("edicts", "error", r"ED_Alloc: no free edicts",
          "Out of entities (edicts)",
          "The map plus plugin entities (supply boxes, effects, dropped weapons) exceed the "
          "edict limit: raise it (-num_edicts on ReHLDS) or remove entities (Entities).",
          "entities"),
    _rule("memory", "error",
          r"(?:Hunk_Alloc|Cache_TryAlloc|Z_Malloc|Hunk_AllocName): (?:failed|bad size)",
          "Out of engine memory",
          "Large models and maps exhaust the heap: start the server with a bigger -heapsize "
          "and trim oversized textures (Download).", "download"),
    _rule("overflow", "warning",
          r"(?:SZ_GetSpace: overflow|Reliable channel overflowed|overflowed for)",
          "Network buffer overflow",
          "Too much reliable data at once — often thousands of resources sent on connect or "
          "a plugin spamming messages. Fewer precached files help.", "budget"),
    _rule("too_many_resources", "error",
          r"too many resources",
          "Too many resources for the client list",
          "The client resource list holds 1280 entries: precache fewer files.", "budget"),
    _rule("amxx_module", "error",
          r"Plugin \"?(?P<subject>[\w.-]+\.amxx)\"? failed to load:\s*Module/Library "
          r"\"?(?P<detail>[\w-]+)\"?",
          "An AMXX plugin needs a module that is not loaded",
          "Install / enable the module (modules.ini) — ReAPI plugins need reapi_amxx.", ""),
    _rule("amxx_failed", "error",
          r"Plugin \"?(?P<subject>[\w.-]+\.amxx)\"? failed to load",
          "An AMXX plugin failed to load",
          "Check the reason after 'failed to load' (bad load, version, missing native).", ""),
    _rule("amxx_runtime", "warning",
          r"\[AMXX\] Run time error (?P<detail>\d+)[^(]*\(plugin \"(?P<subject>[\w.-]+)\"\)",
          "An AMXX plugin hit a run-time error",
          "A plugin bug (index out of bounds, native error): the debug trace below it in "
          "the log names the function. Report it to the plugin's author.", ""),
    _rule("host_error", "error", r"Host_Error:\s*(?P<detail>.+)",
          "Host_Error (the server stopped the map)",
          "The text after Host_Error says what broke; other findings here usually explain "
          "it.", ""),
    _rule("fatal", "error", r"FATAL ERROR|Segmentation fault|SIGSEGV",
          "The server crashed",
          "A crash: the lines just before it in the log (and any finding above) are the "
          "best lead; a crash right after a precache usually means a broken model.", ""),
)


@dataclass
class Finding:
    rule: Rule
    subject: str = ""  # the file / plugin the lines name ("" when none)
    detail: str = ""
    count: int = 0
    first_line: int = 0
    source: str = ""  # the log file
    example: str = ""
    lines: list[int] = field(default_factory=list)


def analyze(text: str, source: str = "") -> list[Finding]:
    """Findings of one log text, worst first (errors, then by count)."""
    found: dict[tuple[str, str], Finding] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        for rule in RULES:
            match = rule.pattern.search(line)
            if match is None:
                continue
            groups = match.groupdict()
            subject = (groups.get("subject") or "").strip("'\"").replace("\\", "/")
            detail = (groups.get("detail") or "").strip()
            if rule.key == "host_error" and any(
                    r.pattern.search(line) for r in RULES if r.key != "host_error"):
                continue  # a Host_Error a more specific rule explains
            key = (rule.key, subject.lower() if subject else detail.lower()[:80])
            finding = found.get(key)
            if finding is None:
                finding = found[key] = Finding(rule, subject, detail, 0, number, source,
                                               line.strip()[:300])
            finding.count += 1
            if len(finding.lines) < 50:
                finding.lines.append(number)
            break  # one rule per line: the first (most specific) wins
    order = {"error": 0, "warning": 1}
    return sorted(found.values(),
                  key=lambda f: (order[f.rule.severity], RULES.index(f.rule), -f.count,
                                 f.subject.lower()))


def log_files(folder: Path, newest: int = 20) -> list[Path]:
    """The newest ``*.log`` files of ``folder`` (and its ``logs/``)."""
    folder = Path(folder)
    candidates = [p for base in (folder, folder / "logs") if base.is_dir()
                  for p in base.glob("*.log")]
    candidates += [p for p in (folder / "addons" / "amxmodx" / "logs").glob("*.log")] \
        if (folder / "addons" / "amxmodx" / "logs").is_dir() else []
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[:newest]


def analyze_files(paths: list[Path]) -> list[Finding]:
    """Findings of several logs, merged by rule and subject."""
    merged: dict[tuple[str, str], Finding] = {}
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for finding in analyze(text, Path(path).name):
            key = (finding.rule.key, (finding.subject or finding.detail).lower())
            if key in merged:
                merged[key].count += finding.count
            else:
                merged[key] = finding
    order = {"error": 0, "warning": 1}
    return sorted(merged.values(),
                  key=lambda f: (order[f.rule.severity], RULES.index(f.rule), -f.count,
                                 f.subject.lower()))


def report(findings: list[Finding]) -> str:
    lines = []
    for f in findings:
        what = f" {f.subject}" if f.subject else (f" {f.detail}" if f.detail else "")
        where = f" ({f.source}:{f.first_line})" if f.source else f" (line {f.first_line})"
        lines.append(f"[{f.rule.severity}] {f.rule.title}{what} ×{f.count}{where}")
        lines.append(f"    {f.rule.advice}")
    return "\n".join(lines) + "\n"


__all__ = ["Finding", "RULES", "Rule", "analyze", "analyze_files", "log_files", "report"]
