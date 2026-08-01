// Created by Crowbar 0.74

// Stored modelname (without quotes): "v_Anaconda.mdl"
$modelname "v_anaconda.mdl"
$cd "."
$cdtexture "."
$cliptotextures
$scale 1.0

$bodygroup "weapon"
{
	studio "ref_Anaconda"
}
$bodygroup "hands"
{
	studio "grafted_male"
	studio "grafted_female"
}

$flags 0


$attachment 0 "Bone_Lefthand" 2.6 -8.8 1.4
$attachment 1 "Bone_Lefthand" 2.6 58.799999 -11.4

$cbox 0 0 0 0 0 0

$bbox 0 0 0 0 0 0

$hbox 0 "Bone04" -1.98 -8.88 -1.02 1.73 0.03 1.09
$hbox 0 "Bone_Righthand" -1.34 -2.98 -0.41 1.43 0 0.63
$hbox 0 "Bone01" -1.72 -8.9 -1.02 1.99 0.02 1.09
$hbox 0 "Bone_Lefthand" -1.52 -3.557875 -0.41 1.25 0 0.709668

$sequence "idle1" {
	"v_anaconda_anims\idle1"
	fps 16
}
$sequence "shoot1" {
	"v_anaconda_anims\shoot1"
	{ event 5001 1 "21" }
	{ event 5004 15 "weapons/ana_foley1.wav" }
	fps 30
}
$sequence "shoot2" {
	"v_anaconda_anims\shoot2"
	{ event 5001 1 "21" }
	{ event 5004 15 "weapons/ana_foley1.wav" }
	fps 30
}
$sequence "shoot_empty" {
	"v_anaconda_anims\shoot_empty"
	{ event 5001 0 "21" }
	{ event 5004 1 "weapons/ana_foley1.wav" }
	fps 40
}
$sequence "reload" {
	"v_anaconda_anims\reload"
	{ event 5004 0 "weapons/ana_foley2.wav" }
	{ event 5004 17 "weapons/ana_foley4.wav" }
	{ event 5004 39 "weapons/ana_foley3.wav" }
	fps 30
}
$sequence "draw" {
	"v_anaconda_anims\draw"
	{ event 5004 1 "weapons/ana_foley5.wav" }
	fps 30
}
