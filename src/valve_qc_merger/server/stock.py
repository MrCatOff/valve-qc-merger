"""The stock Counter-Strike 1.6 models the game DLL (ReGameDLL) precaches.

Grouped the way a server replaces them: a weapon pack replaces the view
models (and often the player-held / world ones), a ZM server swaps dropped
weapons for a supply box, a skin pack swaps player models. Paths are the
precache names; :func:`existing` keeps those a mod folder really has (a
fresh install has them all, a stripped one may not).
"""

from __future__ import annotations

from pathlib import Path

WEAPONS = ("ak47", "aug", "awp", "c4", "deagle", "elite", "famas", "fiveseven",
           "flashbang", "g3sg1", "galil", "glock18", "hegrenade", "knife", "m249", "m3",
           "m4a1", "mac10", "mp5", "p228", "p90", "scout", "sg550", "sg552", "smokegrenade",
           "tmp", "ump45", "usp", "xm1014")
# weapons with a world model of their own (the knife has none; C4 is the backpack)
WORLD = tuple(w for w in WEAPONS if w not in ("knife", "c4")) + ("backpack", "thighpack")
SHIELD = ("deagle", "fiveseven", "flashbang", "glock18", "hegrenade", "knife", "p228",
          "smokegrenade", "usp")
PLAYERS = ("arctic", "gign", "gsg9", "guerilla", "leet", "sas", "terror", "urban", "vip")
SHELLS = ("pshell", "rshell", "rshell_big", "shotgunshell")

# key -> (title, what replacing them is for, paths)
GROUPS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "v": ("View models (v_)", "your weapon plugin gives every weapon its own view model",
          tuple(f"models/v_{w}.mdl" for w in WEAPONS)),
    "p": ("Player-held models (p_)", "your plugin sets its own p_ model for every weapon",
          tuple(f"models/p_{w}.mdl" for w in WEAPONS)),
    "w": ("World models (w_)", "dropped weapons use another model (e.g. a supply box on "
          "ZM servers) — replace them with it", tuple(f"models/w_{w}.mdl" for w in WORLD)),
    "shield": ("Tactical shield", "the shield is disabled on your server",
               tuple(f"models/{k}_shield_{w}.mdl" for k in ("v", "p") for w in SHIELD)
               + ("models/w_shield.mdl", "models/p_shield.mdl")),
    "players": ("Player models", "a skin plugin replaces every stock player model",
                tuple(f"models/player/{p}/{p}.mdl" for p in PLAYERS)),
    "shells": ("Shell casings", "your weapons eject their own shells (or none)",
               tuple(f"models/{s}.mdl" for s in SHELLS)),
}


def existing(paths: tuple[str, ...] | list[str], game_dir: Path | None) -> list[str]:
    """``paths`` that are files under ``game_dir`` (all of them without one)."""
    if not game_dir or not Path(game_dir).is_dir():
        return list(paths)
    return [p for p in paths if (Path(game_dir) / p).is_file()]


__all__ = ["GROUPS", "PLAYERS", "SHELLS", "SHIELD", "WEAPONS", "WORLD", "existing"]
