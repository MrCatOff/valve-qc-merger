"""Budget figures of a build's output parts (Qt-free).

For every QC a build emitted: bones, bodyparts, submodels, sequences,
textures and the largest per-sequence animation stream (studiomdl's 64K
cap, via the exact replica in :mod:`~valve_qc_merger.merge_view.animsize`),
next to the limits. Computed once after a run and stored in
``last_run.json`` so the panel opens instantly.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from valve_qc_merger.merge_view.animsize import SEQ_DATA_LIMIT, sequence_sizes
from valve_qc_merger.merge_view.discovery import load_model
from valve_qc_merger.retarget.qc_build import parse_bodygroups, parse_sequences
from valve_qc_merger.studio.model_info import _entries_with_blanks

LIMITS = {
    "bones": 127,
    "bodyparts": 32,
    "submodels": 32,
    "sequences": 255,  # a view model's animation index is a byte
    "textures": 100,
    "seq_bytes": SEQ_DATA_LIMIT,
}


@dataclass
class PartStats:
    name: str
    qc: str  # relative to the project root
    bones: int
    bodyparts: int
    submodels: int
    sequences: int
    textures: int
    seq_bytes: int
    seq_bytes_name: str
    error: str = ""

    def over(self) -> list[str]:
        """Names of the figures above their limit."""
        return [key for key, limit in LIMITS.items() if getattr(self, key) > limit]


def part_stats(qc: Path, root: Path) -> PartStats:
    """Measure one emitted part (its QC and the SMDs it references)."""
    relative = qc.relative_to(root).as_posix() if qc.is_relative_to(root) else str(qc)
    try:
        model = load_model(qc.parent, require_anims=False)
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        return PartStats(qc.stem, relative, 0, 0, 0, 0, 0, 0, "", error=str(exc))
    text = qc.read_text(encoding="latin-1")
    groups = _entries_with_blanks(text, parse_bodygroups(text))
    fullest = max(model.meshes.values(), key=lambda m: len(m.nodes), default=None)
    materials = {t.material.lower() for m in model.meshes.values() for t in m.triangles}
    sizes = sequence_sizes([model]) if model.anims else {}
    worst = max(sizes.items(), key=lambda kv: kv[1], default=(("", ""), 0))
    return PartStats(
        name=qc.stem,
        qc=relative,
        bones=len(fullest.nodes) if fullest is not None else 0,
        bodyparts=len(groups),
        submodels=sum(len(entries) for entries in groups.values()),
        sequences=len(parse_sequences(text)),
        textures=len(materials),
        seq_bytes=worst[1],
        seq_bytes_name=worst[0][1],
    )


def record_part_stats(record_path: Path, root: Path) -> list[PartStats]:
    """Measure every output of a ``last_run.json`` and store the figures in it."""
    record = json.loads(record_path.read_text(encoding="utf-8"))
    stats = [part_stats(root / qc, root) for qc in record.get("outputs", [])
             if qc.lower().endswith(".qc")]
    record["parts"] = [asdict(s) for s in stats]
    record_path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    return stats


def load_record(record_path: Path) -> dict[str, Any] | None:
    if not record_path.exists():
        return None
    return json.loads(record_path.read_text(encoding="utf-8"))


def manifest_rows(output_dir: Path) -> tuple[list[str], list[list[str]]]:
    """The build's per-model manifest (models.ini/json/toml) as a table:
    header + one row per model (``model``, ``pev_body`` first, then anims)."""
    data: dict[str, dict[str, Any]] = {}
    if (output_dir / "models.json").exists():
        data = json.loads((output_dir / "models.json").read_text(encoding="utf-8"))
    elif (output_dir / "models.toml").exists():
        import tomllib
        data = tomllib.loads((output_dir / "models.toml").read_text(encoding="utf-8"))
    elif (output_dir / "models.ini").exists():
        import configparser
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str  # type: ignore[assignment,method-assign]
        parser.read(output_dir / "models.ini", encoding="utf-8")
        data = {s: dict(parser[s]) for s in parser.sections()}
    data = {k: v for k, v in data.items() if not k.startswith("textures")}
    keys: list[str] = []
    for values in data.values():
        for key in values:
            if key not in keys:
                keys.append(key)
    front = [k for k in ("model", "pev_body", "hands") if k in keys]
    rest = [k for k in keys if k not in front]
    header = ["asset", *front, *rest]
    rows = [[name, *(str(values.get(k, "")) for k in front + rest)]
            for name, values in data.items()]
    return header, rows


__all__ = ["LIMITS", "PartStats", "load_record", "manifest_rows", "part_stats",
           "record_part_stats"]
