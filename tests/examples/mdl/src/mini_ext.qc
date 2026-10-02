$modelname "mini_ext.mdl"
$cd "."
$cdtexture "."
$cliptotextures
$externaltextures
$texrendermode "glass.bmp" masked
$bodygroup "body"
{
	studio "body"
}
$bodygroup "gun"
{
	blank
	studio "gun"
}
$attachment 0 "gun" 1 2 3
$controller 0 "gun" XR -30 30
$hbox 1 "gun" -1 -1 -1 2 2 2
$sequence "idle" {
	"anims/idle"
	fps 15
	loop
	ACT_IDLE 1
	{ event 5004 1 "weapons/x.wav" }
}
$sequence "shoot" {
	"anims/shoot"
	fps 30
	{ event 5001 0 "20" }
}
