"""QC regeneration tests (Phase 5 convenience)."""

from __future__ import annotations

from valve_qc_merger.retarget.qc_build import (
    build_qc,
    parse_attachments,
    parse_bodygroups,
    parse_sequences,
    parse_texrendermodes,
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
    assert '"anims/idle"' in qc
    assert '"anims/shoot_right1"' in qc
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


_MULTIPART_QC = """
$modelname "v_bloodhunter.mdl"
$bodygroup "hands"
{
	studio "CSO_Hand_Male_L_2009"
}
$bodygroup "weapon"
{
	studio "v_bloodhunter_left"
}
$bodygroup "weapon"
{
	studio "v_bloodhunter_right01"
}
$bodygroup "weapon"
{
	studio "v_bloodhunter_right02"
}
$texrendermode "bloodhunter_glass.bmp" additive
$texrendermode "bloodhunter_ef01.bmp" additive
$sequence "idle" {
	"v_bloodhunter_anims\\idle"
	fps 30
}
"""


def test_multipart_weapon_emits_one_bodygroup_per_part() -> None:
    # A weapon split across several always-on $bodygroup "weapon" studios keeps
    # each part its own submodel (under the 2048-vertex engine cap): one
    # $bodygroup "weapon" per part, in order, then the shared hands bodygroup.
    qc = build_qc(
        _MULTIPART_QC, mesh_stem="v_bloodhunter_left", anims_subdir="anims",
        surviving_bones=set(), model_name="v_bloodhunter.mdl",
        hand_bodies=["hands_female", "hands_male"],
        weapon_bodies=["v_bloodhunter_left", "v_bloodhunter_right01",
                       "v_bloodhunter_right02"],
    )
    assert qc.count('$bodygroup "weapon"') == 3
    for part in ("v_bloodhunter_left", "v_bloodhunter_right01", "v_bloodhunter_right02"):
        assert f'\tstudio "{part}"' in qc
    assert qc.count('$bodygroup "hands"') == 1
    # weapon groups precede the hands group
    assert qc.rindex('$bodygroup "weapon"') < qc.index('$bodygroup "hands"')


def test_build_qc_carries_texrendermodes() -> None:
    # Effect meshes (additive muzzle flash / blood glass) render wrong without
    # their $texrendermode; the regenerated QC must carry them.
    qc = build_qc(
        _MULTIPART_QC, mesh_stem="v_bloodhunter_left", anims_subdir="anims",
        surviving_bones=set(), model_name="v_bloodhunter.mdl",
        weapon_bodies=["v_bloodhunter_left"],
    )
    assert '$texrendermode "bloodhunter_glass.bmp" additive' in qc
    assert '$texrendermode "bloodhunter_ef01.bmp" additive' in qc


def test_parse_texrendermodes() -> None:
    modes = parse_texrendermodes(_MULTIPART_QC)
    assert modes == [
        '$texrendermode "bloodhunter_glass.bmp" additive',
        '$texrendermode "bloodhunter_ef01.bmp" additive',
    ]


def test_parse_bodygroups_keeps_duplicate_weapon_blocks() -> None:
    groups = parse_bodygroups(_MULTIPART_QC)
    assert groups["weapon"] == ["v_bloodhunter_left"]
    assert groups["weapon_2"] == ["v_bloodhunter_right01"]
    assert groups["weapon_3"] == ["v_bloodhunter_right02"]
