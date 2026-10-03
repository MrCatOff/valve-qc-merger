/*
==============================================================================
v_zombibomb_witch.mdl
==============================================================================
*/

$modelname v_zombibomb_witch.mdl
$cd .
$cdtexture ./maps_8bit

$texrendermode frogbomb.bmp masked

$body studio "witch_boom_set"
$body studio "witch_set"

$sequence idle {
    "./anims/idle"
    fps 30
    loop
    { event 5004 1 "Zombi/Zombi_Bomb_Idle_1.wav" }
    { event 5004 50 "Zombi/Zombi_Bomb_Idle_2.wav" }
}
$sequence pullpin {
    "./anims/pullpin"
    fps 30
    { event 5001 1 "31" }
    { event 5004 6 "Zombi/Zombi_Bomb_Pull_1.wav" }
}
$sequence throw {
    "./anims/throw"
    fps 40
    { event 5004 1 "Zombi/Zombi_Bomb_Throw.wav" }
}
$sequence deploy {
    "./anims/deploy"
    fps 30
    { event 5004 5 "Zombi/Zombi_Bomb_deploy.wav" }
}
