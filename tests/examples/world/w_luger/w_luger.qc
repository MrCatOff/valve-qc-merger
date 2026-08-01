// Created by Crowbar 0.74

$modelname "w_luger.mdl"
$cd "."
$cdtexture "."
$cliptotextures
$scale 1.0

$bodygroup "studio"
{
	studio "w_ref"
}
$bodygroup "hands"
{
	studio "upgrade"
	studio "upgrade_2"
}

$flags 0

$texturegroup "skinfamilies"
{
	{ "#256256Luger_P_08_Old_p.bmp" }
	{ "#256256Luger_p_6.bmp"        }
	{ "#256256Luger_p_8.bmp"        }
}


$cbox 0 0 0 0 0 0

$bbox 0 0 0 0 0 0

$hbox 0 "Luger_P_08_Low" -2.36 -10.5 -1.06 7.84 2.26 0.85

$sequence "idle" {
	"w_luger_anims\idle"
	fps 30
}
