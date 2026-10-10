/*
 * VQM Weapons — puts the merged models of a valve-qc-merger package on the
 * stock CS weapons (ReHLDS + ReGameDLL + ReAPI). A starting point: read it,
 * test it on your server, change what your mod needs.
 *
 * Install
 *   1. Copy vqm_resources.inc (Export server package ▸ amxx/) next to this file
 *      in scripting/ and compile: amxxpc vqm_weapons.sma
 *   2. vqm_weapons.amxx -> plugins/, add it to plugins.ini (after reapi).
 *   3. vqm_weapons.ini -> configs/, one line per stock weapon:
 *
 *        weapon_ak47 = v_ak47long_hands p_ak47long w_ak47long weapons/ak47long-1.wav
 *
 *      the manifest names of its v_, p_ and w_ model ("-" keeps the stock one)
 *      and, optionally, the shot sound. Changing the ini needs no recompile.
 *
 * What it does
 *   - deploy: the view and player models of the line; the weapon's own body
 *     selects its submodel in the merged model;
 *   - every animation the game sends is renumbered for the merged model (the
 *     source model's n-th sequence is the stock weapon's animation n) and sent
 *     with the body — also to players with cl_lw 1;
 *   - with a shot sound: the client's own prediction and the stock fire event
 *     are switched off for that weapon (they would play the stock animation
 *     numbers, with body 0); the plugin plays a shoot sequence of the merged
 *     model and the sound for everyone instead (no shells / smoke then);
 *   - dropped weapons (weaponbox) get the w_ model and body.
 *
 * Shared-hands view models (merge-v --shared-hands) select the hands with the
 * low dimension: body = (weapon + 1) x 3 + 1 for the first hands (the value
 * the manifest gives) — set vqm_hand 1 for the second hands (+1). Every
 * bodygroup of a merged view model leads with a blank, so body 0 draws
 * nothing while the client predicts the stock animations.
 * The p_ model is drawn with the PLAYER's body: vqm_p_body 1 sets it on deploy
 * (it also selects the player model's own bodygroups — turn it off when your
 * player models use them).
 */

#include <amxmodx>
#include <fakemeta>
#include <hamsandwich>
#include <reapi>
#include "vqm_resources.inc"

#define PLUGIN  "VQM Weapons"
#define VERSION "1.0"
#define AUTHOR  "valve-qc-merger"

const WEAPON_SLOTS = 32;
const MAX_EVENTS = 512;

new g_v[WEAPON_SLOTS] = { -1, ... };
new g_p[WEAPON_SLOTS] = { -1, ... };
new g_w[WEAPON_SLOTS] = { -1, ... };
new g_fire_sound[WEAPON_SLOTS][64];
new g_event_weapon[MAX_EVENTS];
new g_clip[MAX_PLAYERS + 1];
new g_fw_precache_event;
new g_cvar_p_body, g_cvar_hand;

public plugin_precache()
{
	vqm_precache();
	load_config();
	g_fw_precache_event = register_forward(FM_PrecacheEvent, "fw_PrecacheEvent_Post", 1);
}

public plugin_init()
{
	register_plugin(PLUGIN, VERSION, AUTHOR);
	unregister_forward(FM_PrecacheEvent, g_fw_precache_event, 1);
	g_cvar_p_body = register_cvar("vqm_p_body", "1");
	g_cvar_hand = register_cvar("vqm_hand", "0");

	RegisterHookChain(RG_CBasePlayerWeapon_DefaultDeploy, "rg_DefaultDeploy_Pre", false);
	RegisterHookChain(RG_CBasePlayerWeapon_SendWeaponAnim, "rg_SendWeaponAnim_Pre", false);
	RegisterHookChain(RG_CWeaponBox_SetModel, "rg_WeaponBox_SetModel_Pre", false);
	register_forward(FM_UpdateClientData, "fw_UpdateClientData_Post", 1);
	register_forward(FM_PlaybackEvent, "fw_PlaybackEvent");

	new classname[32];
	for (new csw = 1; csw < WEAPON_SLOTS; csw++)
	{
		if (g_v[csw] < 0 || !g_fire_sound[csw][0] || !get_weaponname(csw, classname, charsmax(classname)))
			continue;
		RegisterHam(Ham_Weapon_PrimaryAttack, classname, "ham_PrimaryAttack_Pre", false);
		RegisterHam(Ham_Weapon_PrimaryAttack, classname, "ham_PrimaryAttack_Post", true);
	}
}

/* -- configuration ------------------------------------------------------- */

load_config()
{
	new path[256];
	get_localinfo("amxx_configsdir", path, charsmax(path));
	add(path, charsmax(path), "/vqm_weapons.ini");
	new file = fopen(path, "rt");
	if (!file)
	{
		log_amx("%s not found: no weapon changed", path);
		return;
	}
	new line[256], key[32], rest[224], v[64], p[64], w[64], sound[64];
	while (fgets(file, line, charsmax(line)))
	{
		trim(line);
		if (!line[0] || line[0] == ';' || line[0] == '/' || line[0] == '#')
			continue;
		strtok2(line, key, charsmax(key), rest, charsmax(rest), '=', TRIM_FULL);
		new csw = get_weaponid(key);
		if (csw <= 0 || csw >= WEAPON_SLOTS)
		{
			log_amx("vqm_weapons.ini: unknown weapon ^"%s^"", key);
			continue;
		}
		v[0] = p[0] = w[0] = sound[0] = EOS;
		parse(rest, v, charsmax(v), p, charsmax(p), w, charsmax(w), sound, charsmax(sound));
		g_v[csw] = lookup(v, key);
		g_p[csw] = lookup(p, key);
		g_w[csw] = lookup(w, key);
		if (sound[0] && !equal(sound, "-"))
		{
			copy(g_fire_sound[csw], charsmax(g_fire_sound[]), sound);
			precache_sound(sound);
		}
	}
	fclose(file);
}

lookup(const name[], const weapon[])
{
	if (!name[0] || equal(name, "-"))
		return -1;
	new row = vqm_find(name);
	if (row < 0)
		log_amx("vqm_weapons.ini: %s: no ^"%s^" in vqm_resources.inc", weapon, name);
	return row;
}

/* events/<name>.sc of each weapon with a shot sound: the fire event to block */
public fw_PrecacheEvent_Post(type, const name[])
{
	new index = get_orig_retval();
	if (index <= 0 || index >= MAX_EVENTS)
		return;
	new classname[32];
	for (new csw = 1; csw < WEAPON_SLOTS; csw++)
	{
		if (g_v[csw] < 0 || !g_fire_sound[csw][0] || !get_weaponname(csw, classname, charsmax(classname)))
			continue;
		if (event_matches(name, classname[7]))  // past "weapon_"
		{
			g_event_weapon[index] = csw;
			return;
		}
	}
}

/* "events/ak47.sc" ~ "ak47"; "events/mp5n.sc" ~ "mp5navy"; "events/elite_left.sc" ~ "elite" */
bool:event_matches(const name[], const short[])
{
	new base[32];
	copy(base, charsmax(base), name[7]);  // past "events/"
	new dot = contain(base, ".");
	if (dot > 0)
		base[dot] = EOS;
	new len = strlen(base) < strlen(short) ? strlen(base) : strlen(short);
	return len > 0 && equali(base, short, len);
}

/* -- view and player models --------------------------------------------- */

view_body(row)
{
	return VQM_BODIES[row] + get_pcvar_num(g_cvar_hand);
}

send_anim(player, anim, body)
{
	set_entvar(player, var_weaponanim, anim);
	message_begin(MSG_ONE, SVC_WEAPONANIM, _, player);
	write_byte(anim);
	write_byte(body);
	message_end();
}

/* (this, szViewModel[], szWeaponModel[], iAnim, szAnimExt[], skiplocal) */
public rg_DefaultDeploy_Pre(const weapon, const view[], const held[], anim, const ext[], skiplocal)
{
	new csw = get_member(weapon, m_iId);
	if (csw <= 0 || csw >= WEAPON_SLOTS)
		return HC_CONTINUE;
	if (g_v[csw] >= 0)
	{
		SetHookChainArg(2, ATYPE_STRING, VQM_MODEL_PATHS[g_v[csw]]);
		set_entvar(weapon, var_body, view_body(g_v[csw]));
	}
	if (g_p[csw] >= 0)
	{
		SetHookChainArg(3, ATYPE_STRING, VQM_MODEL_PATHS[g_p[csw]]);
		if (get_pcvar_num(g_cvar_p_body))
		{
			new player = get_member(weapon, m_pPlayer);
			set_entvar(player, var_body, VQM_BODIES[g_p[csw]]);
			set_entvar(player, var_skin, VQM_SKINS[g_p[csw]]);
		}
	}
	return HC_CONTINUE;
}

/* (this, iAnim, skiplocal): every animation the game sends, renumbered and
   sent with the body, also to cl_lw 1 players */
public rg_SendWeaponAnim_Pre(const weapon, anim, skiplocal)
{
	new csw = get_member(weapon, m_iId);
	if (csw <= 0 || csw >= WEAPON_SLOTS || g_v[csw] < 0)
		return HC_CONTINUE;
	new row = g_v[csw];
	if (anim >= 0 && anim < VQM_SEQ_COUNT[row])
		anim = VQM_SEQ[VQM_SEQ_FIRST[row] + anim];
	send_anim(get_member(weapon, m_pPlayer), anim, view_body(row));
	return HC_SUPERCEDE;
}

/* -- shots: no client prediction, no stock event; our sequence and sound -- */

active_weapon(player)
{
	if (!is_user_alive(player))
		return 0;
	new item = get_member(player, m_pActiveItem);
	if (is_nullent(item))
		return 0;
	new csw = get_member(item, m_iId);
	return (csw > 0 && csw < WEAPON_SLOTS) ? csw : 0;
}

public fw_UpdateClientData_Post(player, sendweapons, cd)
{
	new csw = active_weapon(player);
	if (csw && g_v[csw] >= 0 && g_fire_sound[csw][0])
		set_cd(cd, CD_flNextAttack, get_gametime() + 0.001);
}

public fw_PlaybackEvent(flags, invoker, eventid)
{
	if (eventid <= 0 || eventid >= MAX_EVENTS || !g_event_weapon[eventid])
		return FMRES_IGNORED;
	if (invoker < 1 || invoker > MaxClients || active_weapon(invoker) != g_event_weapon[eventid])
		return FMRES_IGNORED;
	return FMRES_SUPERCEDE;
}

public ham_PrimaryAttack_Pre(weapon)
{
	new player = get_member(weapon, m_pPlayer);
	if (player >= 1 && player <= MaxClients)
		g_clip[player] = get_member(weapon, m_Weapon_iClip);
}

public ham_PrimaryAttack_Post(weapon)
{
	new player = get_member(weapon, m_pPlayer);
	new csw = get_member(weapon, m_iId);
	if (player < 1 || player > MaxClients || get_member(weapon, m_Weapon_iClip) >= g_clip[player])
		return;  // no shot (empty clip, not ready)
	new row = g_v[csw];
	if (VQM_SHOOT_COUNT[row] > 0)
		send_anim(player, VQM_SHOOT[VQM_SHOOT_FIRST[row] + random(VQM_SHOOT_COUNT[row])],
			view_body(row));
	emit_sound(player, CHAN_WEAPON, g_fire_sound[csw], VOL_NORM, ATTN_NORM, 0, PITCH_NORM);
}

/* -- dropped weapons ------------------------------------------------------ */

/* (this, const szModelName[]) */
public rg_WeaponBox_SetModel_Pre(const box, const model[])
{
	for (new slot = 0; slot < MAX_ITEM_TYPES; slot++)
	{
		new item = get_member(box, m_WeaponBox_rgpPlayerItems, slot);
		if (is_nullent(item))
			continue;
		new csw = get_member(item, m_iId);
		if (csw > 0 && csw < WEAPON_SLOTS && g_w[csw] >= 0)
		{
			SetHookChainArg(2, ATYPE_STRING, VQM_MODEL_PATHS[g_w[csw]]);
			set_entvar(box, var_body, VQM_BODIES[g_w[csw]]);
			set_entvar(box, var_skin, VQM_SKINS[g_w[csw]]);
			break;
		}
	}
	return HC_CONTINUE;
}
