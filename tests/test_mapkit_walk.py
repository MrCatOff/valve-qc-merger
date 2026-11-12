"""Walking a map as a player: walls stop, stairs are climbed, jumps reach a
crate, crouching gets under a beam, noclip flies through."""

from __future__ import annotations

import numpy as np

from tests.mapkit_fixtures import build_map, room_solids
from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.walk import Controls, Walker

# a room 1024 x 512: a wall across x = 0 with a doorway at y > 128; a step
# 16 high and a crate 40 high on the left; a beam 46 over the floor on the right
STEP = ((-300.0, -256.0, 0.0), (-200.0, -150.0, 16.0))
CRATE = ((-300.0, 150.0, 0.0), (-200.0, 256.0, 40.0))
BEAM = ((200.0, -256.0, 46.0), (240.0, 256.0, 256.0))
WALL = ((-8.0, -256.0, 0.0), (8.0, 128.0, 256.0))


def _map() -> B.Bsp:
    solids, outer = room_solids(walls=[WALL, STEP, CRATE, BEAM])
    return B.Bsp.parse(build_map(solids=solids, outer=outer))


def _run(walker: Walker, seconds: float, controls: Controls) -> None:
    for _ in range(int(seconds * 60)):
        walker.step(1 / 60, controls)


def test_a_wall_stops_the_player() -> None:
    walker = Walker.at_spawn(_map(), (-100.0, 0.0, 36.0), yaw=0.0)
    _run(walker, 2.0, Controls(forward=1))
    assert abs(walker.origin[0] - (-24.0)) < 0.5 and walker.on_ground and not walker.stuck


def test_stairs_are_climbed_and_crates_need_a_jump() -> None:
    bsp = _map()
    stairs = Walker.at_spawn(bsp, (-100.0, -200.0, 36.0), yaw=180.0)
    highest = 0.0
    for _ in range(60):
        stairs.step(1 / 60, Controls(forward=1))
        highest = max(highest, float(stairs.origin[2]))
    assert highest > 36.0 + 15.0  # up on the 16-unit step (and off its far side later)
    crate = Walker.at_spawn(bsp, (-100.0, 200.0, 36.0), yaw=180.0)
    _run(crate, 1.5, Controls(forward=1))
    assert crate.origin[2] < 40.0 and crate.origin[0] > -190  # 40 units: no walking up
    top = 0.0
    for frame in range(90):
        crate.step(1 / 60, Controls(forward=1, jump=frame % 30 < 2))
        if crate.on_ground:
            top = max(top, float(crate.origin[2]))
    assert top > 36.0 + 39.0  # jumped up on it and stood there


def test_crouching_gets_under_a_beam() -> None:
    bsp = _map()
    upright = Walker.at_spawn(bsp, (100.0, 0.0, 36.0), yaw=0.0)
    _run(upright, 1.5, Controls(forward=1))
    assert upright.origin[0] < 200 - 15  # the beam at 46 stops a standing player
    ducked = Walker.at_spawn(bsp, (100.0, 0.0, 36.0), yaw=0.0)
    _run(ducked, 3.0, Controls(forward=1, duck=True))
    assert ducked.origin[0] > 240 and ducked.ducked
    _run(ducked, 1.0, Controls(forward=1))  # stands up again once clear
    assert not ducked.ducked


def test_falling_and_noclip() -> None:
    bsp = _map()
    falling = Walker.at_spawn(bsp, (-100.0, 0.0, 200.0))
    assert not falling.on_ground
    _run(falling, 1.5, Controls())
    assert falling.on_ground and abs(falling.origin[2] - 36.0) < 1.0
    ghost = Walker.at_spawn(bsp, (-100.0, 0.0, 36.0), yaw=0.0)
    ghost.noclip = True
    _run(ghost, 0.5, Controls(forward=1))
    assert ghost.origin[0] > 100  # through the wall
    assert "noclip" in ghost.status()


def test_a_spawn_in_a_wall_is_told() -> None:
    walker = Walker.at_spawn(_map(), (0.0, 0.0, 36.0))
    assert walker.stuck and "STUCK" in walker.status()
    assert np.allclose(walker.eye - walker.origin, [0, 0, 17])
