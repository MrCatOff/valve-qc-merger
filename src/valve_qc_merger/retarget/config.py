"""Configuration for the retargeting pipeline.

Every knob in ``TECHNICAL_SPECIFICATIONS_V2.md`` lives here, including the §11
author decisions (recorded explicitly so they are reversible rather than
assumed). The config is a plain dataclass with no ``bpy`` dependency: the driver
loads it, then serialises the resolved values into each worker job so the
in-Blender worker never imports this module.

Defaults encode the §11 recommendations:

* anchor policy   -> ``"wrist"``           (§11.1)
* weapon offset   -> zero                  (§11.2)
* re-rig allowed  -> ``False``             (§11.3, the "no re-rig" constraint)
* frustum policy  -> ``"warn"``            (§11.4)
* finger priority -> pinky..thumb          (§11.5, relaxation order)
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

AnchorPolicy = str  # "wrist" | "root"
FrustumPolicy = str  # "warn" | "fail" | "trim"


@dataclass(frozen=True)
class JointLimit:
    """Per-axis Euler rotation limits, in degrees (§7.6)."""

    min: tuple[float, float, float]
    max: tuple[float, float, float]


@dataclass(frozen=True)
class SolverConfig:
    """Grip-solve (Phase 4) weights, tolerances and limits (§7.6, §8)."""

    w_pos: float = 1.0
    w_dir: float = 0.3
    lambda_temporal: float = 0.05
    lambda_base: float = 0.02
    damping: float = 0.1
    max_iterations: int = 40
    tau_pos: float = 0.15  # tip error / distal phalanx length (§7.6)
    tau_depth: float = 0.05  # over-penetration slack vs the original hand (§7.6)
    contact_band: tuple[float, float] = (0.5, 2.0)  # overlap_ref / overlap_src (§7.6)
    # Flexion-dominant defaults; MCP allows small abduction (§7.6).
    limit_mcp: JointLimit = JointLimit((-20.0, -10.0, -10.0), (90.0, 10.0, 10.0))
    limit_pip: JointLimit = JointLimit((0.0, 0.0, 0.0), (100.0, 0.0, 0.0))
    limit_dip: JointLimit = JointLimit((0.0, 0.0, 0.0), (100.0, 0.0, 0.0))


@dataclass(frozen=True)
class CameraConfig:
    """Viewmodel camera used by the frustum guard (§7.4)."""

    fov_y_degrees: float = 90.0
    aspect: float = 4.0 / 3.0
    near: float = 0.1
    far: float = 128.0


@dataclass(frozen=True)
class RetargetConfig:
    """Resolved pipeline configuration."""

    fps: float = 30.0
    w_min: float = 0.05  # weight threshold for hand/weapon classification (§5)

    # --- §11 author decisions ---------------------------------------------
    anchor_policy: AnchorPolicy = "wrist"  # §11.1
    weapon_offset: tuple[float, float, float] | None = None  # §11.2 (None => zero)
    allow_rerig: bool = False  # §11.3
    frustum_policy: FrustumPolicy = "warn"  # §11.4
    finger_priority: tuple[str, ...] = ("pinky", "ring", "middle", "index", "thumb")  # §11.5

    solver: SolverConfig = field(default_factory=SolverConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)

    seed: int = 0
    epsilon: float = 1e-5  # format-level equality tolerance (Phase 6)
    euler_jump_threshold_degrees: float = 120.0  # §7.8 continuity

    # Environment / pinning (§6). Recorded in the report.
    blender: str | None = None  # explicit Blender executable; None => autodiscover

    def to_job_dict(self) -> dict[str, Any]:
        """Serialise for embedding in a worker job (plain JSON types)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RetargetConfig:
        """Build a config from a nested mapping (e.g. parsed TOML)."""
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")

        kwargs: dict[str, Any] = dict(data)
        if "weapon_offset" in kwargs and kwargs["weapon_offset"] is not None:
            kwargs["weapon_offset"] = tuple(kwargs["weapon_offset"])
        if "finger_priority" in kwargs:
            kwargs["finger_priority"] = tuple(kwargs["finger_priority"])
        if "solver" in kwargs:
            kwargs["solver"] = _solver_from_dict(kwargs["solver"])
        if "camera" in kwargs:
            kwargs["camera"] = CameraConfig(**kwargs["camera"])
        return cls(**kwargs)

    @classmethod
    def from_toml(cls, path: str | Path) -> RetargetConfig:
        """Load a config from a TOML file (empty/missing keys take defaults)."""
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
        return cls.from_dict(data)


def _solver_from_dict(data: dict[str, Any]) -> SolverConfig:
    kwargs = dict(data)
    for axis_key in ("limit_mcp", "limit_pip", "limit_dip"):
        if axis_key in kwargs:
            limit = kwargs[axis_key]
            kwargs[axis_key] = JointLimit(tuple(limit["min"]), tuple(limit["max"]))
    if "contact_band" in kwargs:
        kwargs["contact_band"] = tuple(kwargs["contact_band"])
    return SolverConfig(**kwargs)


__all__ = ["RetargetConfig", "SolverConfig", "CameraConfig", "JointLimit"]
