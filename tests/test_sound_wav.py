"""WAV checks and fixes against what GoldSource plays."""

from __future__ import annotations

import io
import struct
import wave

import numpy as np
import pytest

from valve_qc_merger.sound.wav import (
    FixOptions,
    WavError,
    engine_rate,
    fix,
    problems,
    read_wav,
    waveform,
    write_wav,
)


def _riff(fmt: bytes, data: bytes, extra: bytes = b"") -> bytes:
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(data)) + data + extra)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _pcm24_stereo(rate: int = 48000, seconds: float = 0.1) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    left = np.sin(2 * np.pi * 440 * t) * 0.5
    right = np.sin(2 * np.pi * 220 * t) * 0.25
    ints = (np.column_stack([left, right]) * (1 << 23)).astype(np.int32).ravel()
    raw = b"".join(int(v & 0xFFFFFF).to_bytes(3, "little") for v in ints)
    fmt = struct.pack("<HHIIHH", 1, 2, rate, rate * 6, 6, 24)
    cue = struct.pack("<I", 1) + struct.pack("<II4sIII", 1, 2400, b"data", 0, 0, 2400)
    return _riff(fmt, raw, b"cue " + struct.pack("<I", len(cue)) + cue)


def test_reads_24_bit_stereo_and_reports_what_the_engine_dislikes() -> None:
    wav = read_wav(_pcm24_stereo())
    assert (wav.channels, wav.rate, wav.bits, wav.frames) == (2, 48000, 24, 4800)
    assert wav.cues == [2400] and wav.loops
    assert abs(wav.peak - 0.5) < 0.01
    found = problems(wav)
    assert any("24-bit" in p for p in found)
    assert any("2 channels" in p for p in found)
    assert any("48000 Hz" in p for p in found)


def test_fix_makes_mono_16_bit_engine_rate_and_moves_the_cue() -> None:
    fixed = fix(read_wav(_pcm24_stereo()), FixOptions(normalize_db=-1.0))
    with wave.open(io.BytesIO(fixed)) as w:  # a plain PCM file for any reader
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 44100)
    again = read_wav(fixed)
    assert problems(again) == []
    assert again.cues == [round(2400 * 44100 / 48000)]
    assert abs(again.peak - 10 ** (-1 / 20)) < 0.01


def test_fix_keeps_a_valid_rate_and_trims_silence() -> None:
    rate = 22050
    tone = np.sin(np.linspace(0, 40, 2000)) * 0.6
    padded = np.concatenate([np.zeros(500), tone, np.zeros(700)])[:, None]
    wav = read_wav(write_wav(padded, rate))
    trimmed = read_wav(fix(wav, FixOptions(trim=True)))
    assert trimmed.rate == rate and 1900 <= trimmed.frames <= 2001


def test_compressed_and_broken_files() -> None:
    adpcm = _riff(struct.pack("<HHIIHH", 2, 1, 22050, 11025, 256, 4), b"\0" * 512)
    wav = read_wav(adpcm)
    assert wav.samples is None and "MS ADPCM" in problems(wav)[0]
    with pytest.raises(WavError):
        fix(wav)
    with pytest.raises(WavError):
        read_wav(b"not a wave file at all")


def test_engine_rate_and_waveform() -> None:
    assert [engine_rate(r) for r in (8000, 11025, 16000, 32000, 48000)] == \
        [11025, 11025, 22050, 44100, 44100]
    wav = read_wav(write_wav(np.linspace(-1, 1, 1000)[:, None], 11025))
    peaks = waveform(wav, 10)
    assert peaks.shape == (10, 2) and peaks[0, 0] < -0.9 and peaks[-1, 1] > 0.9
