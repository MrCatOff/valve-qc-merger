// Created by Crowbar 0.74

$modelname "p_luger.mdl"
$cd "."
$cdtexture "."
$cliptotextures
$scale 1.0

$bodygroup "studio"
{
	studio "p_ref"
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


$attachment 0 "flash" 0 0 0

$cbox 0 0 0 0 0 0

$bbox 0 0 0 0 0 0

$hbox 0 "Luger_P_08_Low" -7.97 -1.56 -0.63 0.85 3.16 0.63

$sequence "idle" {
	"p_luger_anims\idle"
	fps 30
}
