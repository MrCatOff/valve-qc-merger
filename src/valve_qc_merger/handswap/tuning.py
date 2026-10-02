"""Per-weapon grip tuning: ``grip_tuning.json`` next to the CSO hands asset.

Keyed by weapon folder basename::

    {"v_deagle": {"grip_offset": {"left": [0, 0, -0.6]},
                  "weapon_offset": [0, 0.5, 0]}}

``grip_offset`` shifts one palm in palm axes (X fingers-forward, Y toward the
thumb, Z palm normal); ``weapon_offset`` moves the weapon relative to both
hands in model space at the grip frame. Explicit retarget options override
the table (per side for grip offsets, as a whole for the weapon offset).
"""

from __future__ import annotations

import json
import os

FILE_NAME = "grip_tuning.json"
SIDES = ("left", "right")


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
    return out


def save_entry(path: str, weapon: str, *, grip_offset: dict | None = None,
               weapon_offset: list | None = None) -> dict:
    """Replace ``weapon``'s offsets (zero vectors are dropped; an empty
    entry removes the weapon). Other weapons and keys are kept. Returns the
    stored entry."""
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


__all__ = ["FILE_NAME", "load_entry", "save_entry", "tuning_path"]
