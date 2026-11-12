"""A player walking a map the way the game moves one, roughly: for looking
around a map from a spawn — where one gets stuck, falls through, cannot
climb out, hits his head.

:class:`Walker` is the engine's player movement cut down (``PM_PlayerMove``
of the HLSDK): gravity 800, run speed 250, ground friction 4, acceleration
5 on the ground and 10 in the air, a jump to 45 units, steps of 18 units,
sliding along walls, crouching (hull 3, a third of the speed). It moves
through hull 1 / hull 3 of the world with :func:`hull.trace_hull`, so it
stops where a player stops; brush entities (doors, func_wall …) are not in
the world's hulls and are walked through. ``noclip`` flies through
everything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.hull import (
    CONTENTS,
    CROUCH,
    EMPTY,
    SOLID,
    STAND,
    Trace,
    point_contents,
    trace_hull,
)

GRAVITY = 800.0
MAX_SPEED = 250.0  # a knife in hand
DUCK_SPEED = 0.333
ACCELERATE = 5.0
AIR_ACCELERATE = 10.0
FRICTION = 4.0
STOP_SPEED = 75.0
JUMP_SPEED = math.sqrt(2 * GRAVITY * 45.0)
STEP = 18.0
NOCLIP_SPEED = 600.0
VIEW_STAND = 17.0  # Counter-Strike's eye over the origin
VIEW_DUCK = 12.0
DUCK_DROP = 18.0  # the origin moves down by this much when ducking on the ground


@dataclass
class Controls:
    forward: float = 0.0  # -1 back … 1 forward
    side: float = 0.0  # -1 left … 1 right
    up: float = 0.0  # noclip: -1 down … 1 up
    jump: bool = False
    duck: bool = False


@dataclass
class Walker:
    bsp: B.Bsp
    origin: np.ndarray
    yaw: float = 0.0  # degrees, 0 = +X
    pitch: float = 0.0  # degrees, positive looks down (as the game)
    velocity: np.ndarray = field(default_factory=lambda: np.zeros(3))
    ducked: bool = False
    on_ground: bool = False
    noclip: bool = False
    stuck: bool = False
    _jump_held: bool = False

    @classmethod
    def at_spawn(cls, bsp: B.Bsp, origin: tuple[float, float, float], yaw: float = 0.0
                 ) -> Walker:
        walker = cls(bsp, np.array(origin, np.float64) + [0.0, 0.0, 1.0], yaw)
        walker.stuck = not walker.fits(walker.origin)
        walker._categorize()
        return walker

    # -- what is around -----------------------------------------------------------

    @property
    def hull(self) -> int:
        return CROUCH if self.ducked else STAND

    @property
    def eye(self) -> np.ndarray:
        return self.origin + [0.0, 0.0, VIEW_DUCK if self.ducked else VIEW_STAND]

    def forward(self) -> np.ndarray:
        y, p = math.radians(self.yaw), math.radians(self.pitch)
        return np.array([math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), -math.sin(p)])

    def fits(self, origin: np.ndarray, hull: int | None = None) -> bool:
        return bool(point_contents(self.bsp, origin[None], hull or self.hull)[0] == EMPTY)

    def contents(self) -> str:
        """What the eye is in (empty, water, lava …)."""
        value = int(point_contents(self.bsp, self.eye[None], 0)[0])
        return CONTENTS.get(value, str(value))

    def _trace(self, start: np.ndarray, end: np.ndarray, hull: int | None = None) -> Trace:
        return trace_hull(self.bsp, start, end, hull or self.hull)

    def _categorize(self) -> None:
        if self.velocity[2] > 180:
            self.on_ground = False
            return
        trace = self._trace(self.origin, self.origin - [0.0, 0.0, 2.0])
        self.on_ground = trace.fraction < 1.0 and trace.normal[2] >= 0.7 and not trace.all_solid

    # -- moving ---------------------------------------------------------------------

    def look(self, dyaw: float, dpitch: float) -> None:
        self.yaw = (self.yaw + dyaw) % 360.0
        self.pitch = max(-89.0, min(89.0, self.pitch + dpitch))

    def step(self, dt: float, controls: Controls) -> None:
        """Move for ``dt`` seconds (call often: 1/60 s steps)."""
        dt = min(max(dt, 0.0), 0.05)
        if self.noclip:
            wish = (self.forward() * controls.forward + self._right() * controls.side
                    + np.array([0.0, 0.0, controls.up]))
            self.origin = self.origin + wish * NOCLIP_SPEED * dt
            self.velocity[:] = 0.0
            self.stuck = False
            return
        self._duck(controls.duck)
        self._categorize()
        if controls.jump and not self._jump_held and self.on_ground:
            self.velocity[2] = JUMP_SPEED
            self.on_ground = False
        self._jump_held = controls.jump
        if self.on_ground:
            self._friction(dt)
        flat_forward = np.array([math.cos(math.radians(self.yaw)),
                                 math.sin(math.radians(self.yaw)), 0.0])
        wish = flat_forward * controls.forward + self._right() * controls.side
        speed = float(np.linalg.norm(wish))
        if speed > 0:
            wish /= speed
        wish_speed = MAX_SPEED * min(speed, 1.0) * (DUCK_SPEED if self.ducked else 1.0)
        if self.on_ground:
            self._accelerate(wish, wish_speed, ACCELERATE, dt)
            self.velocity[2] = 0.0
            self._walk(dt)
        else:
            self._accelerate(wish, min(wish_speed, 30.0), AIR_ACCELERATE, dt)
            self.velocity[2] -= GRAVITY * dt
            self._fly(dt)
        self._categorize()
        self.stuck = not self.fits(self.origin)

    def _right(self) -> np.ndarray:
        y = math.radians(self.yaw)
        return np.array([math.sin(y), -math.cos(y), 0.0])

    def _friction(self, dt: float) -> None:
        speed = float(np.linalg.norm(self.velocity[:2]))
        if speed < 0.1:
            self.velocity[:2] = 0.0
            return
        drop = max(speed, STOP_SPEED) * FRICTION * dt
        self.velocity[:2] *= max(speed - drop, 0.0) / speed

    def _accelerate(self, wish: np.ndarray, wish_speed: float, accel: float, dt: float) -> None:
        current = float(self.velocity @ wish)
        add = wish_speed - current
        if add <= 0:
            return
        self.velocity += wish * min(accel * wish_speed * dt, add)

    def _fly(self, dt: float) -> float:
        """Slide along what is hit (up to four planes); returns the time left."""
        left = dt
        planes: list[np.ndarray] = []
        for _bump in range(4):
            if not self.velocity.any():
                break
            end = self.origin + self.velocity * left
            trace = self._trace(self.origin, end)
            if trace.all_solid:
                self.velocity[:] = 0.0
                return 0.0
            if trace.fraction > 0:
                self.origin = trace.end.copy()
            if trace.fraction == 1.0:
                return 0.0
            left *= 1.0 - trace.fraction
            planes.append(trace.normal.copy())
            for normal in planes:
                backoff = float(self.velocity @ normal)
                if backoff < 0:
                    self.velocity -= normal * backoff * 1.001
            if len(planes) >= 2 and float(self.velocity @ planes[-2]) < 0:
                crease = np.cross(planes[-2], planes[-1])
                norm = float(np.linalg.norm(crease))
                if norm > 1e-6:
                    crease /= norm
                    self.velocity = crease * float(self.velocity @ crease)
        return left

    def _walk(self, dt: float) -> None:
        """A ground move, stepping up what is no higher than a stair."""
        start, start_v = self.origin.copy(), self.velocity.copy()
        self._fly(dt)
        down, down_v = self.origin.copy(), self.velocity.copy()
        self.origin, self.velocity = start.copy(), start_v.copy()
        up = self._trace(start, start + [0.0, 0.0, STEP])
        if not up.all_solid:
            self.origin = up.end.copy()
        self._fly(dt)
        drop = self._trace(self.origin, self.origin - [0.0, 0.0, STEP + 2.0])
        if not drop.all_solid:
            self.origin = drop.end.copy()
        stepped = drop.fraction < 1.0 and drop.normal[2] >= 0.7
        if not stepped or np.linalg.norm((down - start)[:2]) >= \
                np.linalg.norm((self.origin - start)[:2]):
            self.origin, self.velocity = down, down_v
        else:
            self.velocity[2] = down_v[2]

    def _duck(self, want: bool) -> None:
        if want and not self.ducked:
            self.ducked = True
            if self.on_ground:
                self.origin = self.origin - [0.0, 0.0, DUCK_DROP]
                if not self.fits(self.origin):
                    self.origin = self.origin + [0.0, 0.0, DUCK_DROP]
        elif not want and self.ducked:
            lifted = self.origin + ([0.0, 0.0, DUCK_DROP] if self.on_ground else [0.0, 0.0, 0.0])
            if self.fits(lifted, STAND):
                self.origin, self.ducked = lifted, False

    def status(self) -> str:
        where = ("noclip" if self.noclip else "STUCK — a player does not fit here" if self.stuck
                 else "on the ground" if self.on_ground else "in the air")
        speed = float(np.linalg.norm(self.velocity[:2]))
        x, y, z = (float(v) for v in self.origin)
        return (f"{x:.0f} {y:.0f} {z:.0f} · {speed:.0f} u/s · {where}"
                + (" · crouching" if self.ducked else "") + f" · eye in {self.contents()}")


__all__ = ["Controls", "Walker", "SOLID"]
