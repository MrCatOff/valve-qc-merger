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
    zero = (0.0,) * 6

    # Pass 1: per bone-channel min/max delta over every sequence.
    minv: dict[tuple[str, int], float] = {}
    maxv: dict[tuple[str, int], float] = {}
    for model in models:
        for anim in model.anims.values():
            names = {n.index: n.name for n in anim.nodes}
            for frame in anim.frames:
                for pose in frame.poses:
                    name = names[pose.bone]
                    dflt = defaults.get(name, zero)
                    for c, value in enumerate((
                        pose.position.x, pose.position.y, pose.position.z,
                        pose.rotation.x, pose.rotation.y, pose.rotation.z,
                    )):
                        v = value - dflt[c]
                        if c >= 3:
                            v = _wrap(v)
                        key = (name, c)
                        floor = _POS_FLOOR if c < 3 else _ROT_FLOOR
                        if v < minv.get(key, -floor):
                            minv[key] = v
                        elif v > maxv.get(key, floor):
                            maxv[key] = v

    scales: dict[tuple[str, int], float] = {}

    def scale(name: str, c: int) -> float:
        key = (name, c)
        cached = scales.get(key)
        if cached is None:
            floor = _POS_FLOOR if c < 3 else _ROT_FLOOR
            lo = minv.get(key, -floor)
            hi = maxv.get(key, floor)
            cached = lo / -32768.0 if -lo > hi else hi / 32767.0
            scales[key] = cached
        return cached

    # Pass 2: quantize each sequence's channels and sum RLE sizes.
    sizes: dict[tuple[str, str], int] = {}
    for model in models:
        for seq_name, anim in model.anims.items():
            names = {n.index: n.name for n in anim.nodes}
            channels: dict[tuple[str, int], list[int]] = {}
            for frame in anim.frames:
                for pose in frame.poses:
                    name = names[pose.bone]
                    dflt = defaults.get(name, zero)
                    for c, value in enumerate((
                        pose.position.x, pose.position.y, pose.position.z,
                        pose.rotation.x, pose.rotation.y, pose.rotation.z,
                    )):
                        v = value - dflt[c]
                        if c >= 3:
                            v = _wrap(v)
                        channels.setdefault((name, c), []).append(
                            int(v / scale(name, c))
                        )
            total = 12 * nbones
            for values in channels.values():
                total += rle_bytes(values)
            sizes[(model.name, seq_name)] = total
    return sizes


__all__ = ["SEQ_DATA_LIMIT", "rle_bytes", "sequence_sizes"]
