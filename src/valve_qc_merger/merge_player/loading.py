"""Load decompiled p_/w_ models (shared by merge-p, merge-w and their gates)."""

from __future__ import annotations

from pathlib import Path

from valve_qc_merger.merge_view.discovery import ModelInput, load_model
from valve_qc_merger.parsers.smd import parse_smd_file


def load_player_model(model_dir: Path) -> ModelInput:
    """Load a p_ model; QCs without ``$sequence`` fall back to an on-disk idle.

    Some decompiles (p_tknife) ship an anims folder the QC never references;
    an animation matching the model's own skeleton is still the best source
    for the weapon bone's in-game pose, so adopt the first one that parses.
    """
    model = load_model(model_dir, require_anims=False)
    if not model.anims:
        bones = set(model.bone_names)
        for candidate in sorted(model_dir.glob("*/*.smd")):
            try:
                smd = parse_smd_file(candidate)
            except (OSError, ValueError):
                continue
            if smd.frames and {n.name for n in smd.nodes} <= bones:
                model.anims[candidate.stem] = smd
                model.warnings.append(
                    f"QC has no $sequence; using {candidate.name} for the "
                    "idle pose"
                )
                break
    return model


__all__ = ["load_player_model"]
