"""What a map is checked against: Counter-Strike 1.6's entity classes, the
engine's limits, and the thresholds a profile sets.

``CS_CLASSES`` are the classes ReGameDLL links (``LINK_ENTITY_TO_CLASS``
in its ``dlls/``): an entity of any other class is dropped when the map
loads ("Can't init <class>") — CSO's ``particle_emitter`` and the like.

The defaults below are the ``zp`` profile (a Zombie Plague server of 32
players); a rules file (JSON, ``configs/mapkit.json`` is an example)
overrides any of them, globally or per profile::

    {"profile": "zp",
     "profiles": {"zp": {"min_spawns": 48}, "public": {"min_spawns": 16}},
     "max_players": 32}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path

CS_CLASSES = frozenset((
    "ambient_generic", "ammo_338magnum", "ammo_357sig", "ammo_45acp", "ammo_50ae",
    "ammo_556nato", "ammo_556natobox", "ammo_57mm", "ammo_762nato", "ammo_9mm", "ammo_buckshot",
    "armoury_entity", "beam", "bodyque", "bot", "button_target", "cycler", "cycler_prdroid",
    "cycler_sprite", "cycler_weapon", "cycler_wreckage", "env_beam", "env_beverage",
    "env_blood", "env_bombglow", "env_bubbles", "env_debris", "env_explosion", "env_fade",
    "env_fog", "env_funnel", "env_global", "env_glow", "env_laser", "env_lightning",
    "env_message", "env_rain", "env_render", "env_shake", "env_shooter", "env_snow",
    "env_sound", "env_spark", "env_sprite", "fireanddie", "func_bomb_target", "func_breakable",
    "func_button", "func_buyzone", "func_conveyor", "func_door", "func_door_rotating",
    "func_escapezone", "func_friction", "func_grencatch", "func_guntarget",
    "func_healthcharger", "func_hostage_rescue", "func_illusionary", "func_ladder",
    "func_monsterclip", "func_mortar_field", "func_pendulum", "func_plat", "func_platrot",
    "func_pushable", "func_rain", "func_recharge", "func_rot_button", "func_rotating",
    "func_snow", "func_tank", "func_tankcontrols", "func_tanklaser", "func_tankmortar",
    "func_tankrocket", "func_trackautochange", "func_trackchange", "func_tracktrain",
    "func_train", "func_traincontrols", "func_vehicle", "func_vehiclecontrols",
    "func_vip_safetyzone", "func_wall", "func_wall_toggle", "func_water", "func_weaponcheck",
    "game_counter", "game_counter_set", "game_end", "game_player_equip", "game_player_hurt",
    "game_player_team", "game_score", "game_team_master", "game_team_set", "game_text",
    "game_zone_player", "gib", "gibshooter", "grenade", "hostage_entity", "info_bomb_target",
    "info_hostage_rescue", "info_intermission", "info_landmark", "info_map_parameters",
    "info_null", "info_player_deathmatch", "info_player_start", "info_spawn_point",
    "info_target", "info_teleport_destination", "info_vip_start", "infodecal", "item_airbox",
    "item_airtank", "item_antidote", "item_assaultsuit", "item_battery", "item_healthkit",
    "item_kevlar", "item_longjump", "item_security", "item_sodacan", "item_suit",
    "item_thighpack", "light", "light_environment", "light_spot", "map", "momentary_door",
    "momentary_rot_button", "monster_hevsuit_dead", "monster_mortar", "monster_scientist",
    "multi_manager", "multisource", "path_corner", "path_track", "player", "player_loadsaved",
    "player_weaponstrip", "point_clientcommand", "point_servercommand", "soundent",
    "spark_shower", "speaker", "target_cdaudio", "test_effect", "trigger", "trigger_auto",
    "trigger_autosave", "trigger_camera", "trigger_cdaudio", "trigger_changelevel",
    "trigger_changetarget", "trigger_counter", "trigger_endsection", "trigger_gravity",
    "trigger_hurt", "trigger_monsterjump", "trigger_multiple", "trigger_once", "trigger_push",
    "trigger_random", "trigger_random_time", "trigger_random_unique", "trigger_relay",
    "trigger_setorigin", "trigger_teleport", "trigger_transition", "weapon_ak47", "weapon_aug",
    "weapon_awp", "weapon_c4", "weapon_deagle", "weapon_elite", "weapon_famas",
    "weapon_fiveseven", "weapon_flashbang", "weapon_g3sg1", "weapon_galil", "weapon_glock18",
    "weapon_hegrenade", "weapon_knife", "weapon_m249", "weapon_m3", "weapon_m4a1",
    "weapon_mac10", "weapon_mp5navy", "weapon_p228", "weapon_p90", "weapon_scout",
    "weapon_sg550", "weapon_sg552", "weapon_shield", "weapon_smokegrenade", "weapon_tmp",
    "weapon_ump45", "weapon_usp", "weapon_xm1014", "weaponbox", "world_items", "worldspawn"
))

# models / sprites / sounds a map's entities may name, by key
RESOURCE_KEYS = ("model", "sprite", "message", "noise", "noise1", "noise2", "noise3",
                 "texture", "gibmodel", "shootmodel")


@dataclass
class Rules:
    """Thresholds of a map check (see the module for the file format)."""

    profile: str = "zp"
    min_spawns: int = 48  # per team (info_player_start = CT, info_player_deathmatch = T)
    max_players: int = 32  # fewer spawns than half of this on a team is critical
    num_edicts: int = 0  # 0: the engine's 900 + 15 * (max_players - 1)
    edicts_warn: float = 0.8
    max_leafs: int = 8192
    texture_warn: int = 256  # a side over this: warning (over 2x: critical)
    world_limit: float = 4096.0
    world_margin: float = 64.0  # geometry this close to the limit: warning
    model_slots: int = 512
    sound_slots: int = 512
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def edicts(self) -> int:
        return self.num_edicts or 900 + 15 * (self.max_players - 1)

    @classmethod
    def load(cls, path: Path | None = None, profile: str | None = None) -> Rules:
        data: dict[str, object] = {}
        if path is not None:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        name = profile or str(data.get("profile", "zp"))
        merged = {k: v for k, v in data.items() if k not in ("profile", "profiles")}
        profiles = data.get("profiles", {})
        if isinstance(profiles, dict) and isinstance(profiles.get(name), dict):
            merged.update(profiles[name])
        known = {f.name for f in fields(cls)} - {"extra", "profile"}
        rules = cls(profile=name)
        for key, value in merged.items():
            if key in known:
                setattr(rules, key, type(getattr(rules, key))(value))
            else:
                rules.extra[key] = value
        return rules


def known_class(name: str) -> bool:
    return name.lower() in CS_CLASSES


__all__ = ["CS_CLASSES", "RESOURCE_KEYS", "Rules", "known_class"]
