"""WAV files as GoldSource plays them: read, check against the engine, fix.

The engine mixes 8- or 16-bit PCM at 11025, 22050 or 44100 Hz; sounds placed
in the world must be mono (stereo only plays non-positional, and some
clients refuse it). A ``cue`` point marks where a looping sound restarts
(an ``ltxt`` length in a ``LIST adtl`` chunk, where it ends). Python's
``wave`` module reads only PCM and drops cue chunks, so RIFF is parsed here:
PCM 8/16/24/32-bit, IEEE float and WAVE_FORMAT_EXTENSIBLE are decoded;
compressed formats (ADPCM, MP3-in-WAV...) are recognised and reported.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ENGINE_RATES = (11025, 22050, 44100)
ENGINE_BITS = (8, 16)
FORMAT_PCM, FORMAT_FLOAT, FORMAT_EXTENSIBLE = 1, 3, 0xFFFE
FORMAT_NAMES = {1: "PCM", 2: "MS ADPCM", 3: "IEEE float", 6: "A-law", 7: "µ-law",
                0x11: "IMA ADPCM", 0x55: "MP3", 0xFFFE: "extensible"}
CLIP_LEVEL = 0.999


class WavError(ValueError):
    """Not a readable RIFF/WAVE file."""


@dataclass
class Wav:
    format: int  # the effective format tag (extensible resolved)
    channels: int
    rate: int
    bits: int
    frames: int
    samples: np.ndarray | None  # (frames, channels) float64 in -1..1; None: compressed
    cues: list[int] = field(default_factory=list)  # sample offsets (loop starts)
    loop_length: int | None = None  # samples, from LIST/adtl/ltxt
    size: int = 0

    @property
    def duration(self) -> float:
        return self.frames / self.rate if self.rate else 0.0

    @property
    def peak(self) -> float:
        if self.samples is None or not self.samples.size:
            return 0.0
        return float(np.abs(self.samples).max())

    @property
    def format_name(self) -> str:
        return FORMAT_NAMES.get(self.format, f"format {self.format:#x}")

    @property
    def loops(self) -> bool:
        return bool(self.cues)


def _chunks(data: bytes) -> list[tuple[bytes, int, int]]:
    """(id, start, size) of every top-level chunk of a RIFF/WAVE file."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise WavError("not a RIFF/WAVE file")
    out = []
    cursor = 12
    while cursor + 8 <= len(data):
        cid, size = data[cursor:cursor + 4], struct.unpack_from("<I", data, cursor + 4)[0]
        start = cursor + 8
        out.append((cid, start, min(size, len(data) - start)))
        cursor = start + size + (size & 1)  # chunks are word-aligned
    return out


def read_wav(source: Path | bytes) -> Wav:
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    chunks = _chunks(data)
    fmt = next(((s, n) for cid, s, n in chunks if cid == b"fmt "), None)
    body = next(((s, n) for cid, s, n in chunks if cid == b"data"), None)
    if fmt is None or body is None:
        raise WavError("no fmt or data chunk")
    tag, channels, rate, _byte_rate, block, bits = struct.unpack_from("<HHIIHH", data, fmt[0])
    if tag == FORMAT_EXTENSIBLE and fmt[1] >= 40:
        tag = struct.unpack_from("<H", data, fmt[0] + 24)[0]  # SubFormat GUID's first word
    channels = max(channels, 1)
    raw = data[body[0]:body[0] + body[1]]
    frame_bytes = block or channels * max(bits // 8, 1)
    frames = len(raw) // frame_bytes if frame_bytes else 0
    samples = _decode(raw[:frames * frame_bytes], tag, bits, channels)
    wav = Wav(tag, channels, rate, bits, frames, samples, size=len(data))
    for cid, start, size in chunks:
        if cid == b"cue " and size >= 4:
            (count,) = struct.unpack_from("<I", data, start)
            for i in range(min(count, (size - 4) // 24)):
                wav.cues.append(struct.unpack_from("<I", data, start + 4 + i * 24 + 20)[0])
        elif cid == b"LIST" and data[start:start + 4] == b"adtl":
            cursor = start + 4
            while cursor + 8 <= start + size:
                sub, sub_size = data[cursor:cursor + 4], struct.unpack_from(
                    "<I", data, cursor + 4)[0]
                if sub == b"ltxt" and sub_size >= 8:
                    wav.loop_length = struct.unpack_from("<I", data, cursor + 12)[0]
                cursor += 8 + sub_size + (sub_size & 1)
    return wav


def _decode(raw: bytes, tag: int, bits: int, channels: int) -> np.ndarray | None:
    if tag == FORMAT_PCM and bits == 8:
        values = (np.frombuffer(raw, np.uint8).astype(np.float64) - 128.0) / 128.0
    elif tag == FORMAT_PCM and bits == 16:
        values = np.frombuffer(raw, "<i2").astype(np.float64) / 32768.0
    elif tag == FORMAT_PCM and bits == 24:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        ints = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        ints = np.where(ints >= 1 << 23, ints - (1 << 24), ints)
        values = ints.astype(np.float64) / float(1 << 23)
    elif tag == FORMAT_PCM and bits == 32:
        values = np.frombuffer(raw, "<i4").astype(np.float64) / float(1 << 31)
    elif tag == FORMAT_FLOAT and bits in (32, 64):
        values = np.frombuffer(raw, "<f4" if bits == 32 else "<f8").astype(np.float64)
    else:
        return None  # compressed: the engine cannot play it either
    return values.reshape(-1, channels)


def problems(wav: Wav) -> list[str]:
    """What the engine does not like about ``wav`` (empty: fine)."""
    out = []
    if wav.samples is None or wav.format not in (FORMAT_PCM,):
        out.append(f"{wav.format_name} data: the engine plays only uncompressed PCM")
    elif wav.bits not in ENGINE_BITS:
        out.append(f"{wav.bits}-bit samples: the engine plays 8- or 16-bit PCM")
    if wav.channels > 1:
        out.append(f"{wav.channels} channels: world sounds must be mono (stereo plays "
                   "only non-positional, some clients refuse it)")
    if wav.rate not in ENGINE_RATES:
        out.append(f"{wav.rate} Hz: use 11025, 22050 or 44100 Hz")
    if wav.samples is not None and wav.frames:
        if wav.peak == 0.0:
            out.append("silent: every sample is zero")
        elif np.mean(np.abs(wav.samples) >= CLIP_LEVEL) > 0.001:
            out.append("clipped: samples hit full scale (distorted); normalise lower")
    return out


def wav_problems(path: Path) -> list[str]:
    """``problems`` of a file, or why it cannot be read (doctor's hook)."""
    try:
        return problems(read_wav(Path(path)))
    except (WavError, OSError, struct.error, ValueError) as exc:
        return [f"cannot be read as WAV: {exc}"]


def _resample(samples: np.ndarray, rate: int, target: int) -> np.ndarray:
    """Linear interpolation (adequate for game sound effects)."""
    if rate == target or not len(samples):
        return samples
    count = max(int(round(len(samples) * target / rate)), 1)
    old = np.arange(len(samples)) / rate
    new = np.arange(count) / target
    return np.column_stack([np.interp(new, old, samples[:, c])
                            for c in range(samples.shape[1])])


def engine_rate(rate: int) -> int:
    """The engine rate nearest above ``rate`` (keeps quality), at most 44100."""
    return next((r for r in ENGINE_RATES if r >= rate), ENGINE_RATES[-1])


@dataclass
class FixOptions:
    mono: bool = True
    rate: int | None = None  # None: keep a valid one, else the nearest engine rate
    normalize_db: float | None = None  # peak target in dBFS, e.g. -1.0
    trim: bool = False  # cut leading/trailing silence (loops are never trimmed)
    silence_db: float = -50.0


def fix(wav: Wav, options: FixOptions | None = None) -> bytes:
    """``wav`` as an engine-safe file: 16-bit PCM, mono (by default), an
    engine sample rate, cue points kept (moved with resampling/trimming)."""
    options = options or FixOptions()
    if wav.samples is None:
        raise WavError(f"{wav.format_name} data cannot be converted here (decode it to "
                       "PCM WAV in an audio editor first)")
    samples = wav.samples
    if options.mono and samples.shape[1] > 1:
        samples = samples.mean(axis=1, keepdims=True)
    cues = list(wav.cues)
    loop_length = wav.loop_length
    if options.trim and not cues and len(samples):
        threshold = 10 ** (options.silence_db / 20)
        loud = np.flatnonzero(np.abs(samples).max(axis=1) > threshold)
        if loud.size:
            samples = samples[loud[0]:loud[-1] + 1]
    target = options.rate or (wav.rate if wav.rate in ENGINE_RATES else engine_rate(wav.rate))
    if target != wav.rate:
        scale = target / wav.rate
        samples = _resample(samples, wav.rate, target)
        cues = [int(round(c * scale)) for c in cues]
        loop_length = int(round(loop_length * scale)) if loop_length else loop_length
    if options.normalize_db is not None and samples.size:
        peak = float(np.abs(samples).max())
        if peak > 0:
            samples = samples * (10 ** (options.normalize_db / 20) / peak)
    return write_wav(samples, target, cues=cues, loop_length=loop_length)


def write_wav(samples: np.ndarray, rate: int, *, cues: list[int] | None = None,
              loop_length: int | None = None) -> bytes:
    """16-bit PCM RIFF/WAVE (+ cue / LIST adtl ltxt chunks for loops)."""
    samples = np.atleast_2d(samples)
    if samples.shape[0] == 1 and samples.shape[1] > 2:
        samples = samples.T
    channels = samples.shape[1]
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes()
    fmt = struct.pack("<HHIIHH", FORMAT_PCM, channels, rate, rate * channels * 2,
                      channels * 2, 16)
    chunks = [b"fmt " + struct.pack("<I", len(fmt)) + fmt,
              b"data" + struct.pack("<I", len(pcm)) + pcm + (b"\0" if len(pcm) & 1 else b"")]
    if cues:
        points = b"".join(struct.pack("<II4sIII", i + 1, c, b"data", 0, 0, c)
                          for i, c in enumerate(cues))
        cue = struct.pack("<I", len(cues)) + points
        chunks.append(b"cue " + struct.pack("<I", len(cue)) + cue)
        if loop_length:
            ltxt = struct.pack("<II4sHHHH", 1, loop_length, b"mark", 0, 0, 0, 0)
            adtl = b"adtl" + b"ltxt" + struct.pack("<I", len(ltxt)) + ltxt
            chunks.append(b"LIST" + struct.pack("<I", len(adtl)) + adtl)
    body = b"WAVE" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def waveform(wav: Wav, columns: int) -> np.ndarray:
    """(columns, 2) min/max of the mono mix per column, for drawing."""
    if wav.samples is None or not wav.frames or columns <= 0:
        return np.zeros((max(columns, 0), 2))
    mono = wav.samples.mean(axis=1)
    edges = np.linspace(0, len(mono), columns + 1).astype(int)
    out = np.zeros((columns, 2))
    for i in range(columns):
        part = mono[edges[i]:max(edges[i + 1], edges[i] + 1)]
        if part.size:
            out[i] = part.min(), part.max()
    return out


__all__ = ["ENGINE_BITS", "ENGINE_RATES", "FixOptions", "Wav", "WavError", "engine_rate",
           "fix", "problems", "read_wav", "wav_problems", "waveform", "write_wav"]
