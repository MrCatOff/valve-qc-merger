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


def sequence_sizes(models: list[ModelInput]) -> dict[tuple[str, str], int]:
    """(model name, sequence name) -> compiled anim stream bytes.

    Assumes every SMD was conformed to one identical node table and every
    mesh carries the shared bind as its single skeleton frame (merge-view's
    post-unify state) — exactly what studiomdl will see.
    """
    any_mesh = next(iter(models[0].meshes.values()))
    nbones = len(any_mesh.nodes)
    defaults: list[tuple[float, ...]] = [()] * nbones
    for pose in any_mesh.frames[0].poses:
        defaults[pose.bone] = (
            pose.position.x, pose.position.y, pose.position.z,
            pose.rotation.x, pose.rotation.y, pose.rotation.z,
        )

    # Pass 1: per bone-channel min/max delta over every sequence.
    minv = [(-_POS_FLOOR if c < 3 else -_ROT_FLOOR)
            for _b in range(nbones) for c in range(6)]
    maxv = [(_POS_FLOOR if c < 3 else _ROT_FLOOR)
            for _b in range(nbones) for c in range(6)]
    for model in models:
        for anim in model.anims.values():
            for frame in anim.frames:
                for pose in frame.poses:
                    dflt = defaults[pose.bone]
                    base = pose.bone * 6
                    for c, value in enumerate((
                        pose.position.x, pose.position.y, pose.position.z,
                        pose.rotation.x, pose.rotation.y, pose.rotation.z,
                    )):
                        v = value - dflt[c]
                        if c >= 3:
                            v = _wrap(v)
                        if v < minv[base + c]:
                            minv[base + c] = v
                        elif v > maxv[base + c]:
                            maxv[base + c] = v
    scales = [
        (minv[i] / -32768.0) if -minv[i] > maxv[i] else (maxv[i] / 32767.0)
        for i in range(nbones * 6)
    ]

    # Pass 2: quantize each sequence's channels and sum RLE sizes.
    sizes: dict[tuple[str, str], int] = {}
    for model in models:
        for seq_name, anim in model.anims.items():
            channels: list[list[int]] = [[] for _ in range(nbones * 6)]
            for frame in anim.frames:
                for pose in frame.poses:
                    dflt = defaults[pose.bone]
                    base = pose.bone * 6
                    for c, value in enumerate((
                        pose.position.x, pose.position.y, pose.position.z,
                        pose.rotation.x, pose.rotation.y, pose.rotation.z,
                    )):
                        v = value - dflt[c]
                        if c >= 3:
                            v = _wrap(v)
                        channels[base + c].append(int(v / scales[base + c]))
            total = 12 * nbones
            for values in channels:
                total += rle_bytes(values)
            sizes[(model.name, seq_name)] = total
    return sizes


__all__ = ["SEQ_DATA_LIMIT", "rle_bytes", "sequence_sizes"]
