"""Blender-driven weapon-animation retargeting pipeline.

See ``docs/retarget.md``. The pipeline: a non-``bpy``
driver (:mod:`valve_qc_merger.retarget.driver`) spawns one headless Blender
worker (:mod:`valve_qc_merger.retarget.worker`, run *inside* Blender) per
animation sequence, then verifies the emitted SMDs at the text level
(:mod:`valve_qc_merger.retarget.verify`).
"""

from __future__ import annotations

from valve_qc_merger.retarget.config import RetargetConfig

__all__ = ["RetargetConfig"]
