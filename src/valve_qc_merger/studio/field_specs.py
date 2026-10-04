"""How the studio presents service options: label, help, group, placeholder.

The option dataclasses and the CLI flags speak the command line's language
(``no_verify``, ``--no-snug``, "skip the verification suite"); a form shows
what the user decides instead: a sentence-case label, a tooltip that reads
right next to a check box, ``Basic`` vs ``Advanced``, and an ``invert`` for
negative flags (``no_verify`` is shown as "Verify the result", checked).
Fields the studio sets itself, or that make no sense in it, are ``hidden``
(their stored values are kept untouched). Lookup: ``"<command>.<field>"``
first, then ``"<field>"``; a field without a spec falls back to its name and
the CLI help.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSpec:
    label: str
    help: str = ""
    advanced: bool = False
    invert: bool = False  # a negative flag shown as its positive (checked = False)
    placeholder: str = ""  # for empty/None values ("default" otherwise)
    hidden: bool = False


def _adv(label: str, help: str = "", **kwargs) -> FieldSpec:  # noqa: A002 - field name
    return FieldSpec(label, help, advanced=True, **kwargs)


HIDDEN = FieldSpec("", hidden=True)

SPECS: dict[str, FieldSpec] = {
    # -- every merge ---------------------------------------------------------
    "name": FieldSpec("Output name", "File name of the merged model, without .mdl "
                      "(v_pistols → v_pistols.mdl)."),
    "exclude": _adv("Exclude models", "Model folders to leave out of the merge.",
                    placeholder="e.g. v_knife, v_c4"),
    "manifest_format": _adv("Manifest format", "Format of the per-model manifest "
                            "(which weapon sits at which pev->body) the game plugin reads."),
    "texture_budget": _adv("Textures per part", "Most textures one compiled part may hold "
                           "(the engine's hard cap is 100)."),
    "max_texture_size": _adv("Max texture size", "Downscale textures larger than this many "
                             "pixels on either side (re-quantised to 8-bit).",
                             placeholder="no limit"),
    "pack_textures": _adv("Pack textures into atlases", "Put eligible textures four to a "
                          "512×512 atlas (shared palette, UVs rewritten) to save texture slots."),
    "no_pack_texture": _adv("Keep out of atlases", "Texture names (wildcards allowed) never "
                            "packed into an atlas.", placeholder="e.g. *chrome*, *scope*"),
    "no_verify": _adv("Verify the result", "Run the verification gate after merging "
                      "(geometry, budgets, skeleton). Recommended.", invert=True),
    "dry_run": HIDDEN,
    "plan_only": HIDDEN,  # the Plan button
    # -- merge-v -------------------------------------------------------------
    "merge-v.shared_hands": FieldSpec(
        "Shared hands", "Every model already wears our male/female hands: emit ONE shared "
        "hands bodygroup instead of hands per weapon. Set automatically by \"Put every "
        "model on our hands first\"."),
    "merge-v.skin_variants": FieldSpec(
        "Skins as weapon entries", "Every extra $texturegroup skin becomes a weapon entry of "
        "its own (the server can set pev->body, not the skin). Off: keep only the first skin."),
    "merge-v.sound_path": FieldSpec(
        "Sound path template", "Rewrite the sound event paths of every weapon; "
        "${fileBasename} is the original file name, e.g. csforce/pistols/${fileBasename}.",
        placeholder="keep the original paths"),
    "merge-v.reference": _adv("Hand skeleton", "Canonical hand skeleton SMD the rigs are "
                              "matched to.", placeholder="bundled reference"),
    "merge-v.skip_unmatched": _adv("Skip unmatched rigs", "Leave out models whose rig cannot "
                                   "be matched instead of failing the build."),
    "prune": _adv("Prune unused bones", "Also fold away bones without vertices that nothing "
                  "references (by default only Finger*Nub bones go)."),
    "merge-v.no_pool_bones": _adv("Pool bones", "Share bones between weapons so the merged "
                                  "skeleton stays under the 127-bone limit.", invert=True),
    "merge-v.sequence_budget": _adv("Sequences per part", "Most sequences one compiled part "
                                    "may hold after de-duplication.",
                                    placeholder="111 (255 with shared hands)"),
    "merge-v.max_decimation": _adv("Max fold decimation", "With shared hands, a multi-part "
                                   "weapon is folded into one submodel when that removes at "
                                   "most this fraction of its vertices."),
    # -- merge-players -------------------------------------------------------
    "merge-players.group_by": FieldSpec("Group by", "How player models are split into "
                                        "merges: by body size, team or sex."),
    "merge-players.base": _adv("Donor rig", "Folder with the donor skeleton and the shared "
                               "animations every player body is put on."),
    "merge-players.proportion_tolerance": _adv("Size tolerance", "Size grouping: largest "
                                               "bone-length difference (units) for two rigs "
                                               "to share a merge."),
    "merge-players.labels": _adv("Team / sex labels", "TOML file overriding the detected team "
                                 "and sex per model, e.g. {gign = \"ct\"}.",
                                 placeholder="detected from the names"),
    "merge-players.placeholder_seq": _adv("Placeholder sequences", "Sequence names (wildcards "
                                          "allowed) replaced by a placeholder.",
                                          placeholder="*shield*"),
    "merge-players.include_base": _adv("Include the donor body", "Add the donor's own body "
                                       "as skin 0."),
    "merge-players.max_skins": _adv("Skins per part", "Most skins one output part may hold.",
                                    placeholder="no limit"),
    "merge-players.submodel_limit": _adv("Submodels per bodypart", "Stock studiomdl allows "
                                         "32; raise only for a patched compiler."),
    # -- merge-zhands --------------------------------------------------------
    "merge-zhands.grenade_prefix": _adv("Grenade texture prefix", "Texture-name prefix that "
                                        "marks grenade triangles."),
    "merge-zhands.grenade_texture": _adv("Shared grenade from", "The shared grenade is taken "
                                         "from a model using this texture."),
    # -- retarget (build "on our hands first" and the Retarget dialog) -------
    "retarget.category": HIDDEN,  # a CLI storage bucket; the studio sets the output
    "retarget.studiomdl": HIDDEN,  # Project ▸ Settings
    "retarget.compile": HIDDEN,  # builds compile
    "retarget.snug": FieldSpec("Fit fingers to the weapon", "Curl every finger automatically "
                               "until it touches the weapon."),
    "retarget.snug_max_deg": FieldSpec("Max finger fit (°)", "Per-joint limit of the automatic "
                                       "fit, in degrees.",
                                       placeholder="weapon's grip tuning, else 35"),
    "retarget.verify": _adv("Verify the output", "Check the written files (skeleton, "
                            "trajectories, grip) after retargeting. Recommended."),
    "retarget.asset": _adv("Hands asset", "Our hands to put on (CSO hands asset).",
                           placeholder="bundled male/female hands"),
    "retarget.hands_texture": _adv("Hands texture", "BMP used as the hands texture.",
                                   placeholder="bundled male.bmp"),
    "retarget.modelname": _adv("$modelname", "Override the compiled model's $modelname.",
                               placeholder="from the asset name"),
    "curl": _adv("Extra finger curl", "Per finger and hand: + closes, − opens, in degrees.",
                 placeholder="e.g. left:ForeFinger:+8, right:BigFinger:-4"),
    "grip_offset": _adv("Palm offsets", "Shift one palm in its own axes: x fingers-forward, "
                        "y toward the thumb, z palm normal.",
                        placeholder="e.g. left:0,0,-0.4; right:0.1,0,0"),
    "weapon_offset": _adv("Weapon offset", "Move the weapon relative to both hands, in model "
                          "space at the grip frame (the fingers re-fit afterwards).",
                          placeholder="x, y, z"),
    # -- canonicalize --------------------------------------------------------
    "canonicalize.reference": _adv("Hand skeleton", "Canonical hand skeleton the bones are "
                                   "renamed and reparented to.", placeholder="bundled reference"),
    # -- zhands-grenade ------------------------------------------------------
    "zhands-grenade.donor": FieldSpec("Grenade donor", "Grenade model the frog bomb and the "
                                      "hand animation come from.",
                                      placeholder="bundled banshee grenade"),
    "zhands-grenade.snug_max_deg": FieldSpec("Max finger fit (°)", "Per-joint limit of the "
                                             "automatic finger fit, in degrees.",
                                             placeholder="35"),
    "zhands-grenade.weapon_offset": _adv("Grenade offset", "Move the frog bomb relative to "
                                         "both hands, in model space.", placeholder="x, y, z"),
}


def spec_for(command: str, name: str) -> FieldSpec | None:
    return SPECS.get(f"{command}.{name}") or SPECS.get(name)


def default_label(name: str) -> str:
    return name.replace("_", " ").capitalize()


__all__ = ["FieldSpec", "SPECS", "default_label", "spec_for"]
