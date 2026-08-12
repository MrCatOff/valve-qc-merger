"""merge-players: merge CSO player-character models into skin-bodygrouped CS 1.6 models.

Unlike merge-p (p_ *weapon* models), this merges third-person *player body*
models: many Counter-Strike Nexon (CSO) skins onto ONE canonical CS 1.6 rig.
A donor model (``tmp/ORIGINAL_CS_MODEL``, arctic) supplies the skeleton, the
canonical ~111-sequence animation set (voiding unneeded slots with the
``I_am_a_stupid_placeholder`` trick), hitboxes, attachments and controller; each
CSO body becomes one entry of a single ``skin`` bodygroup, selected by pev_body.

The CSO skeletons are bind-compatible with CS 1.6 (core Bip01 rotations match),
so no per-vertex retarget is needed: CSO-specific sub-bones (Breast_Sub, ...)
collapse onto their parent core bone and every body conforms onto the donor's
exact node table, sharing the donor's animations byte-for-byte.

See ``docs/merge-players.md``.
"""
