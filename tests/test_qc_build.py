"""QC regeneration tests (Phase 5 convenience)."""

from __future__ import annotations

from valve_qc_merger.retarget.qc_build import (
    build_qc,
    parse_attachments,
    parse_bodygroups,
    parse_sequences,
)

_QC = """
$modelname "v_elite.mdl"
$bodygroup "hands"
{
	studio "f_elite_Male_hand_Low"
}
$attachment 0 "Bone63" 0 -5.75 0
$attachment 1 "Bone01" 0 -5.5 0
$hbox 0 "Bone01" -2 -9 -0.9 2 0 1.2
$sequence "idle" {
	"v_elite_anims\\idle"
	fps 16
}
$sequence "shoot_right1" {
	"v_elite_anims\\shoot_right1"
	{ event 5011 0 "11" }
	fps 40
}
"""


def test_parse_sequences_reads_fps_and_events() -> None:
    seqs = parse_sequences(_QC)
    assert [s.name for s in seqs] == ["idle", "shoot_right1"]
    assert seqs[0].fps == 16
    assert seqs[0].events == ()
    assert seqs[1].fps == 40
    assert seqs[1].events == ('{ event 5011 0 "11" }',)


def test_parse_attachments_drops_deleted_bones() -> None:
    kept = parse_attachments(_QC, surviving_bones={"Bone63"})
    assert kept == ['$attachment 0 "Bone63" 0 -5.75 0']  # Bone01 dropped


# The decompiler (tools/decompmdl) writes a different QC dialect: bare (unquoted)
# names, single-line sequences, and a leading "./" on anim paths.
_DECOMPILED_QC = """
$modelname v_elite.mdl
$cd .
$cdtexture ./maps_8bit

$body studio "v_elite-pv"
$bodygroup hands
{
    studio "hands_female"
    studio "hands_male"
}

$attachment 0 "Bone63" 0 -5.75 0

$sequence idle "./anims/idle" fps 16
$sequence shoot_right1 {
    "./anims/shoot_right1"
    fps 40
    { event 5011 0 "11" }
}
"""


def test_parse_decompiled_dialect() -> None:
    # Bare names, single-line + braced bodies, "./anims/..." paths all parse.
    seqs = parse_sequences(_DECOMPILED_QC)
    assert [s.name for s in seqs] == ["idle", "shoot_right1"]
    assert seqs[0].fps == 16 and seqs[0].smd == "./anims/idle"
    assert seqs[1].fps == 40
    assert seqs[1].events == ('{ event 5011 0 "11" }',)
    assert parse_attachments(_DECOMPILED_QC, {"Bone63"}) == ['$attachment 0 "Bone63" 0 -5.75 0']
    assert parse_bodygroups(_DECOMPILED_QC) == {"hands": ["hands_female", "hands_male"]}


def test_build_qc_points_at_merged_mesh_and_anims() -> None:
    qc = build_qc(
        _QC, mesh_stem="v_elite-PV", anims_subdir="anims",
        surviving_bones={"Bone63"}, model_name="v_elite-PV.mdl",
    )
    assert '$body "studio" "v_elite-PV"' in qc
    assert '"anims\\idle"' in qc
    assert '"anims\\shoot_right1"' in qc
    assert '{ event 5011 0 "11" }' in qc
    assert '$attachment 0 "Bone63"' in qc
    assert "Bone01" not in qc  # deleted arm bone must not survive in the QC
    assert "$hbox" not in qc  # hitboxes referenced deleted bones; dropped


def test_bodygroup_render_lists_weapon_and_hand_variants() -> None:
    qc = build_qc(
        _QC, mesh_stem="v_elite-PV", anims_subdir="anims",
        surviving_bones={"Bone63"}, model_name="v_elite.mdl",
        hand_bodies=["hands_female", "hands_male"],
    )
    assert '$bodygroup "weapon"' in qc
    assert '\tstudio "v_elite-PV"' in qc
    assert '$bodygroup "hands"' in qc
    assert '\tstudio "hands_female"' in qc
    assert '\tstudio "hands_male"' in qc
    assert '$body "studio"' not in qc  # bodygroups replace the single body
