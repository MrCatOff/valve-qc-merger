"""Group CSO player models into merge sets by size, team or sex.

The user's rule: models of similar size and hitbox share the CS 1.6 animation
set safely, so they can be merged into one skin-bodygrouped model. Modes:

- ``size`` — the principled default. Cluster by **skeleton proportion** (the
  quantised core-bone lengths). This is the axis the stretch lives on: rigs that
  share a proportion signature can share one animation set with no stretch, so
  the female CSO skeleton, the male one, tankers, monsters and chibi models each
  form their own cluster automatically.
- ``team`` / ``sex`` — partition by a label. Neither team nor sex is encoded in
  the model data, so labels come from a name-keyword table plus an optional
  ``--labels`` override file; unlabeled models are grouped as ``unknown`` and
  logged, never guessed.
"""

from __future__ import annotations

from valve_qc_merger.merge_players.discovery import PlayerModel

# Name-keyword → label tables. Substring match, case-insensitive, first hit wins.
_TEAM_KEYWORDS: list[tuple[str, str]] = [
    ("gign", "ct"), ("gsg9", "ct"), ("sas", "ct"), ("urban", "ct"),
    ("seal", "ct"), ("saf", "ct"), ("sdefence", "ct"), ("spetsnaz", "ct"),
    ("police", "ct"), ("marine", "ct"), ("ct", "ct"),
    ("terror", "t"), ("leet", "t"), ("arctic", "t"), ("guerilla", "t"),
    ("militia", "t"), ("phoenix", "t"), ("mercenarytr", "t"),
    ("pirate", "t"), ("tr", "t"),
]
_FEMALE_KEYWORDS = (
    "girl", "female", "woman", "alice", "natasha", "dominique", "jennifer",
    "choijiyoon", "ritsuka", "heroine", "ira", "yuri", "soi", "vngirl",
    "marinegirl", "idolgirl", "boxxergirl", "jpngirl", "chngirl",
)


def classify_team(name: str) -> str:
    low = name.lower()
    for key, label in _TEAM_KEYWORDS:
        if key in low:
            return label
    return "unknown"


def classify_sex(name: str) -> str:
    low = name.lower()
    if any(k in low for k in _FEMALE_KEYWORDS):
        return "female"
    return "male"


def group_models(
    models: list[PlayerModel],
    *,
    mode: str = "size",
    height_tolerance: float = 0.25,  # kept for CLI/back-compat; unused by size mode
    labels: dict[str, str] | None = None,
) -> list[tuple[str, list[PlayerModel]]]:
    """Partition models into merge groups; returns ``(group_key, members)`` sorted.

    ``labels`` (from ``--labels``) overrides the keyword classifier for team/sex.
    """
    overrides = {k.lower(): v.lower() for k, v in (labels or {}).items()}
    groups: dict[str, list[PlayerModel]] = {}
    for model in models:
        if mode == "size":
            key = model.proportion_sig  # same skeleton proportions == one group
        elif mode == "team":
            key = overrides.get(model.name.lower()) or classify_team(model.name)
        elif mode == "sex":
            key = overrides.get(model.name.lower()) or classify_sex(model.name)
        else:  # pragma: no cover - argparse restricts choices
            raise ValueError(f"unknown group-by mode: {mode!r}")
        groups.setdefault(key, []).append(model)

    return [(key, sorted(groups[key], key=lambda m: m.name)) for key in sorted(groups)]


__all__ = ["group_models", "classify_team", "classify_sex"]
