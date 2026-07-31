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
    # Scalar hinge limits (degrees) per joint depth. The solver constrains each
    # finger to a single flexion axis (the hand's knuckle axis; the thumb gets its
    # own arc plane), so anatomy is structural and these ranges are true flexion
    # bounds rather than per-axis Euler boxes.
    hinge_mcp: tuple[float, float] = (-25.0, 100.0)
    hinge_pip: tuple[float, float] = (0.0, 110.0)
    hinge_dip: tuple[float, float] = (0.0, 90.0)
    hinge_thumb: tuple[float, float] = (-60.0, 90.0)
    # §7.6 temporal regularisation, hinge form: max per-frame joint travel
    # (degrees). Real finger motion stays far below this; a solution that wants
    # to teleport (target crossing the hinge line) is spread over frames instead.
    max_step_degrees: float = 35.0


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
    swap_arms: bool = False  # manual override if auto L/R arm pairing is backwards
    weapon_offset: tuple[float, float, float] | None = None  # §11.2 (None => zero)
    # Constant world translation for the HANDS instead of the weapon: every arm's
    # wrist anchors at source wrist + hand_offset, while the weapon and the grip
    # contact points stay exactly where the animation puts them (§7.5 zero offset
    # preserved). Compensates a hand-size mismatch without re-authoring the gun.
    # None => automatic: shift back along the source rest grip's palm-forward axis
    # by hand_center_fraction of the hand-length difference (ours - original's).
    hand_offset: tuple[float, float, float] | None = None
    # Fraction of the hand-length surplus the auto offset shifts back by. 0.5
    # centres the two hands (the surplus splits evenly behind and ahead of the
    # grip); 0.0 reproduces plain wrist anchoring, 1.0 aligns the fingertips.
    hand_center_fraction: float = 0.5
    allow_rerig: bool = False  # §11.3
    frustum_policy: FrustumPolicy = "warn"  # §11.4
    finger_priority: tuple[str, ...] = ("pinky", "ring", "middle", "index", "thumb")  # §11.5

    solver: SolverConfig = field(default_factory=SolverConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)

    seed: int = 0
    epsilon: float = 1e-5  # format-level equality tolerance (Phase 6)
    euler_jump_threshold_degrees: float = 120.0  # §7.8 continuity
    geom_tolerance: float = 1e-3  # Phase 6 mesh-preservation tolerance (model units)

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
        for offset_key in ("weapon_offset", "hand_offset"):
            if offset_key in kwargs and kwargs[offset_key] is not None:
                kwargs[offset_key] = tuple(kwargs[offset_key])
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
    for pair_key in ("contact_band", "hinge_mcp", "hinge_pip", "hinge_dip", "hinge_thumb"):
        if pair_key in kwargs:
            kwargs[pair_key] = tuple(kwargs[pair_key])
    return SolverConfig(**kwargs)


__all__ = ["RetargetConfig", "SolverConfig", "CameraConfig"]
