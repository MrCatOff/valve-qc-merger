"""Retarget stage 2: interactive weapon-seating + wrist/finger calibration.

When a foreign weapon is retargeted onto our hands, the animation is correct but
the weapon clips into the palm. This package skins the converted model, measures
hand-into-weapon penetration (:mod:`.probe`, numpy-free — reusable by the bake
path), lets a human seat the weapon and correct the fingers (:mod:`.scene`, needs
numpy), and renders it in an interactive view (:mod:`.gui`, imported lazily —
needs the ``[calibrate]`` extra).

Only the numpy-free :class:`~.probe.WeaponMesh` is re-exported here; import
:mod:`.scene` / :mod:`.gui` explicitly so a caller that just needs the probe does
not drag in numpy or the GUI stack.
"""
from __future__ import annotations

from valve_qc_merger.calibrate.probe import WeaponMesh

__all__ = ["WeaponMesh"]
