"""Exact studiomdl animation-size accounting (validated byte-exact).

studiomdl addresses each sequence's RLE stream through u16 offsets in
``mstudioanim_t``, so one sequence's entire stream must fit in 64K; the
compiler hard-errors past that. This module replicates its quantization
(per-bone-channel scale over all sequences, floored at ±128 units / ±π/8 rad,
C truncation toward zero) and its greedy RLE encoder exactly — re-encoding a
shipped model's decoded streams reproduces the original byte counts.
"""

from __future__ import annotations

import math

import numpy as np

from valve_qc_merger.merge_view.discovery import ModelInput

SEQ_DATA_LIMIT = 65535  # u16 offset reach; studiomdl Errors past this

_POS_FLOOR = 128.0
_ROT_FLOOR = math.pi / 8.0


def rle_bytes(values: list[int]) -> int:
    """Byte size of studiomdl's RLE for one channel's quantized shorts."""
    n = len(values)
    if n == 0:
        return 0
    length = 2
    cur_valid, cur_total = 1, 1
    for m in range(1, n):
        if cur_total == 255:
            length += 2
            cur_valid, cur_total = 1, 0
        elif (values[m] != values[m - 1] or
              (cur_total == cur_valid and m < n - 1
               and values[m] != values[m + 1])):
            if cur_total != cur_valid:
                length += 1
                cur_valid, cur_total = 0, 0
            cur_valid += 1
            length += 1
        cur_total += 1
    if length == 2 and values[0] == 0:
        return 0
    return length * 2


def _wrap(v: float) -> float:
    if v >= math.pi:
        return v - 2.0 * math.pi
    if v < -math.pi:
        return v + 2.0 * math.pi
    return v


def _channels(anim, defaults: dict[str, tuple[float, ...]]) -> dict[str, np.ndarray]:
    """Bone name -> (frames, 6) deltas from the bone's default, rotations
    wrapped once into [-pi, pi) — as studiomdl takes them."""
    names = {n.index: n.name for n in anim.nodes}
    rows: dict[str, list[tuple[float, ...]]] = {}
    for frame in anim.frames:
        for pose in frame.poses:
            p, r = pose.position, pose.rotation
            rows.setdefault(names[pose.bone], []).append((p.x, p.y, p.z, r.x, r.y, r.z))
    out: dict[str, np.ndarray] = {}
    for name, values in rows.items():
        delta = np.asarray(values, dtype=np.float64) - np.asarray(
            defaults.get(name, _ZERO), dtype=np.float64)
        rot = delta[:, 3:]
        rot[rot >= math.pi] -= 2.0 * math.pi
        rot[rot < -math.pi] += 2.0 * math.pi
        out[name] = delta
    return out


_ZERO = (0.0,) * 6
_FLOORS = np.array([_POS_FLOOR] * 3 + [_ROT_FLOOR] * 3)


def sequence_sizes(models: list[ModelInput]) -> dict[tuple[str, str], int]:
    """(model name, sequence name) -> compiled anim stream bytes.

    Matches names, not indices, so it works both before and after the SMDs
    are conformed to one table. Bone defaults come from the first model's
    mesh that carries each bone — exactly the first reference file studiomdl
    reads. Bones a sequence lacks are grafted static at those defaults by
    the merge, which compresses to zero bytes, so skipping them is exact.
    """
    defaults: dict[str, tuple[float, ...]] = {}
    for model in models:
        mesh = max(model.meshes.values(), key=lambda m: len(m.nodes))
        names = {n.index: n.name for n in mesh.nodes}
        for pose in mesh.frames[0].poses:
            defaults.setdefault(names[pose.bone], (
                pose.position.x, pose.position.y, pose.position.z,
                pose.rotation.x, pose.rotation.y, pose.rotation.z,
            ))
    nbones = len(defaults)

    # Pass 1: per bone-channel min/max delta over every sequence (floored).
    deltas = [((model.name, seq), _channels(anim, defaults))
              for model in models for seq, anim in model.anims.items()]
    low: dict[str, np.ndarray] = {}
    high: dict[str, np.ndarray] = {}
    for _key, channels in deltas:
        for name, delta in channels.items():
            low[name] = np.minimum(low.get(name, -_FLOORS), delta.min(axis=0))
            high[name] = np.maximum(high.get(name, _FLOORS), delta.max(axis=0))
    scales = {name: np.where(-low[name] > high[name], low[name] / -32768.0,
                             high[name] / 32767.0) for name in low}

    # Pass 2: quantize (C truncation toward zero) and sum the RLE sizes.
    sizes: dict[tuple[str, str], int] = {}
    for key, channels in deltas:
        total = 12 * nbones
        for name, delta in channels.items():
            quantized = np.trunc(delta / scales[name]).astype(np.int64)
            for c in range(6):
                column = quantized[:, c]
                if len(column) <= 255 and (column == column[0]).all():
                    total += 0 if column[0] == 0 else 4  # one run: header + value
                else:
                    total += rle_bytes(column.tolist())
        sizes[key] = total
    return sizes
