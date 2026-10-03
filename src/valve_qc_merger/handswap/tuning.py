"""Per-weapon grip tuning: ``grip_tuning.json`` next to the CSO hands asset.

Keyed by weapon folder basename::

    {"v_deagle": {"grip_offset": {"left": [0, 0, -0.6]},
                  "weapon_offset": [0, 0.5, 0],
                  "curl": {"left": {"ForeFinger": 8}},
                  "snug_max_deg": 30}}

``grip_offset`` shifts one palm in palm axes (X fingers-forward, Y toward the
thumb, Z palm normal); ``weapon_offset`` moves the weapon relative to both
hands in model space at the grip frame; ``curl`` adds degrees per joint to a
finger (+ closes, - opens, either hand); ``snug_max_deg`` is the automatic
finger fit's per-joint limit. Explicit retarget options override the table
(per side for grip offsets, per finger for curls, as a whole otherwise).
"""

from __future__ import annotations

import json
import os

FILE_NAME = "grip_tuning.json"
SIDES = ("left", "right")
FINGERS = ("BigFinger", "ForeFinger", "MiddleFinger", "RingFinger", "PinkyFinger")
_KEEP = object()


def tuning_path(asset_path: str) -> str:
    """The tuning table that belongs to a CSO hands asset."""
    return os.path.join(os.path.dirname(os.path.abspath(asset_path)), FILE_NAME)


def _read(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_entry(path: str, weapon: str) -> dict:
    """``{"grip_offset": {side: [x, y, z]}, "weapon_offset": [x, y, z]}``
    for ``weapon`` (keys absent when unset)."""
    entry = _read(path).get(weapon, {})
    out: dict = {}
    grips = {side: [float(v) for v in xyz]
             for side, xyz in (entry.get("grip_offset") or {}).items()
             if side in SIDES and len(xyz) == 3}
    if grips:
        out["grip_offset"] = grips
    weapon_offset = entry.get("weapon_offset")
    if weapon_offset and len(weapon_offset) == 3 and any(weapon_offset):
        out["weapon_offset"] = [float(v) for v in weapon_offset]
    curl = _clean_curl(entry.get("curl"))
    if curl:
        out["curl"] = curl
    if entry.get("snug_max_deg") is not None:
        out["snug_max_deg"] = float(entry["snug_max_deg"])
    return out


def _clean_curl(curl: object) -> dict:
    out: dict = {}
    for side, fingers in (curl or {}).items() if isinstance(curl, dict) else []:
        if side not in SIDES or not isinstance(fingers, dict):
            continue
        kept = {f: float(d) for f, d in fingers.items() if f in FINGERS and float(d)}
        if kept:
            out[side] = kept
    return out


def curl_specs(curl: dict) -> list[str]:
    """``{"left": {"ForeFinger": 8}}`` -> ``["left:ForeFinger:8"]`` (--curl)."""
    return [f"{side}:{finger}:{deg:g}" for side, fingers in curl.items()
            for finger, deg in fingers.items()]


def save_entry(path: str, weapon: str, *, grip_offset: dict | None = None,
               weapon_offset: list | None = None, curl: object = _KEEP,
               snug_max_deg: object = _KEEP) -> dict:
    """Replace ``weapon``'s offsets (zero vectors are dropped; an empty
    entry removes the weapon); ``curl`` / ``snug_max_deg`` are replaced when
    given (``None`` / ``{}`` remove them). Other weapons and keys are kept.
    Returns the stored entry."""
    data = _read(path)
    entry = dict(data.get(weapon, {}))
    grips = {side: [float(v) for v in xyz] for side, xyz in (grip_offset or {}).items()
             if side in SIDES and len(xyz) == 3 and any(xyz)}
    if grips:
        entry["grip_offset"] = grips
    else:
        entry.pop("grip_offset", None)
    if weapon_offset and any(weapon_offset):
        entry["weapon_offset"] = [float(v) for v in weapon_offset]
    else:
        entry.pop("weapon_offset", None)
    if curl is not _KEEP:
        cleaned = _clean_curl(curl)
        if cleaned:
            entry["curl"] = cleaned
        else:
            entry.pop("curl", None)
    if snug_max_deg is not _KEEP:
        if snug_max_deg is None:
            entry.pop("snug_max_deg", None)
        else:
            entry["snug_max_deg"] = float(snug_max_deg)  # type: ignore[arg-type]
    if entry:
        data[weapon] = entry
    else:
        data.pop(weapon, None)
    comment = {k: v for k, v in data.items() if k.startswith("_")}
    weapons = {k: data[k] for k in sorted(k for k in data if not k.startswith("_"))}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump({**comment, **weapons}, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return entry


__all__ = ["FILE_NAME", "FINGERS", "curl_specs", "load_entry", "save_entry", "tuning_path"]
