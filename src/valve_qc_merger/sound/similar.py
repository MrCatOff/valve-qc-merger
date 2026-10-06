"""Find sounds that could be one file: exact copies, the same sound saved
another way, and merely similar ones.

Three levels, from safe to a judgement call:

- **exact** — the same bytes under two names;
- **same** — the same sound once decoded: another rate, stereo/mono, bit
  depth, gain or silence at the ends (mono 11025 Hz, ends trimmed, peak
  normalised, then cross-correlated: >= 0.97);
- **similar** — not the same recording but close (a rifle's ``clipin`` and
  another's): the time-normalised loudness envelope and the spectrum over
  24 bands, weighted by how close the lengths are. Listen before using one
  for all.

A stock sound (in the game folder, precached by the game DLL anyway) that a
custom one matches is the best one to keep: nothing to download, no extra
slot. :func:`analyse` returns the groups with a suggested keeper.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from valve_qc_merger.sound.wav import WavError, _resample, read_wav

RATE = 11025
LEVELS = ("exact", "same", "similar")
SAME_THRESHOLD = 0.97
SIMILAR_THRESHOLD = 0.9
_FRAME = 256
_BANDS = np.geomspace(60.0, RATE / 2, 25)


@dataclass
class Fingerprint:
    name: str
    md5: str
    samples: np.ndarray  # mono, RATE Hz, ends trimmed, peak 1
    envelope: np.ndarray  # 64 points, time-normalised RMS
    bands: np.ndarray  # 24 mean log band energies, unit length
    grid: np.ndarray  # 24 bands x 32 time steps of log energy (a coarse spectrogram)
    stock: bool = False

    @property
    def duration(self) -> float:
        return len(self.samples) / RATE


def fingerprint(path: Path, name: str, *, stock: bool = False) -> Fingerprint | None:
    """The fingerprint of a WAV (None: unreadable, compressed or silent)."""
    data = Path(path).read_bytes()
    try:
        wav = read_wav(data)
    except (WavError, ValueError):
        return None
    if wav.samples is None or not len(wav.samples) or not wav.rate:
        return None
    mono = _resample(wav.samples.mean(axis=1, keepdims=True), wav.rate, RATE)[:, 0]
    peak = float(np.abs(mono).max())
    if peak <= 1e-6:
        return None
    loud = np.flatnonzero(np.abs(mono) >= peak * 0.02)
    mono = mono[loud[0]:loud[-1] + 1] / peak
    frames = max(len(mono) // _FRAME, 1)
    rms = np.sqrt(np.mean(mono[:frames * _FRAME].reshape(frames, -1) ** 2, axis=1)) \
        if len(mono) >= _FRAME else np.array([np.sqrt(np.mean(mono ** 2))])
    envelope = np.interp(np.linspace(0, len(rms) - 1, 64), np.arange(len(rms)), rms)
    spectrum = np.abs(np.fft.rfft(mono, n=max(1024, 1 << int(np.ceil(np.log2(len(mono)))))))
    freqs = np.fft.rfftfreq(max(1024, 1 << int(np.ceil(np.log2(len(mono))))), 1 / RATE)
    bands = np.array([np.log1p(spectrum[(freqs >= lo) & (freqs < hi)].mean()
                               if np.any((freqs >= lo) & (freqs < hi)) else 0.0)
                      for lo, hi in zip(_BANDS[:-1], _BANDS[1:], strict=True)])
    norm = np.linalg.norm(bands)
    return Fingerprint(name, hashlib.md5(data).hexdigest(), mono, envelope,
                       bands / norm if norm else bands, _grid(mono), stock)


def _grid(mono: np.ndarray, steps: int = 32) -> np.ndarray:
    """Log band energy per time step (24 x ``steps``): what plays when."""
    size, hop = 512, 256
    padded = np.pad(mono, (0, max(0, size - len(mono))))
    count = 1 + (len(padded) - size) // hop
    frames = np.stack([padded[k * hop:k * hop + size] for k in range(count)])
    spectra = np.abs(np.fft.rfft(frames * np.hanning(size), axis=1))
    freqs = np.fft.rfftfreq(size, 1 / RATE)
    bands = np.stack([spectra[:, (freqs >= lo) & (freqs < hi)].mean(axis=1)
                      if np.any((freqs >= lo) & (freqs < hi)) else np.zeros(count)
                      for lo, hi in zip(_BANDS[:-1], _BANDS[1:], strict=True)])
    logs = np.log1p(bands * 10)
    positions = np.linspace(0, count - 1, steps)
    return np.stack([np.interp(positions, np.arange(count), row) for row in logs])


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a - a.mean(), b - b.mean()
    denominator = np.linalg.norm(a) * np.linalg.norm(b)
    return float(a @ b / denominator) if denominator else 0.0


def similarity(a: Fingerprint, b: Fingerprint) -> float:
    """0..1: what plays when (the coarse spectrogram), the loudness shape and
    the overall spectrum, scaled by how close the lengths are."""
    grid = max(_pearson(a.grid.ravel(), b.grid.ravel()), 0.0)
    envelope = max(_pearson(a.envelope, b.envelope), 0.0)
    spectrum = float(np.clip(a.bands @ b.bands, 0.0, 1.0))
    lengths = min(a.duration, b.duration) / max(a.duration, b.duration, 1e-9)
    return (0.6 * grid + 0.2 * envelope + 0.2 * spectrum) * lengths ** 0.5


# what a weapon sound does, from its file name: two sounds that say they do
# different things (a clip going in, a knife hitting) are never "similar"
_ROLES = (
    ("clipin", r"clip_?in|mag_?in|magin|insert_?mag"), ("clipout", r"clip_?out|mag_?out"),
    ("bolt", r"bolt|slide|charge|pump|cock|chamber"), ("draw", r"draw|deploy|select"),
    ("insert", r"insert|shell_?in|load_?shell"), ("dryfire", r"dry_?fire|empty"),
    ("hit", r"hit|stab|slash|swing|melee"), ("zoom", r"zoom|scope"),
    ("shot", r"shoot|fire|shot|-\d+\.wav$|_\d+\.wav$(?<!in\d\.wav)"),
)


def role(name: str) -> str:
    """The role a sound's name gives it ("" when it says nothing clear)."""
    import re
    low = name.lower().rsplit("/", 1)[-1]
    for key, pattern in _ROLES:
        if re.search(pattern, low):
            return key
    return ""


def same_sound(a: Fingerprint, b: Fingerprint) -> float:
    """The best normalised cross-correlation of the trimmed waveforms within
    ±30 ms (0 when the lengths differ by more than 3 % / 20 ms)."""
    la, lb = len(a.samples), len(b.samples)
    if abs(la - lb) > max(0.03 * max(la, lb), 0.02 * RATE):
        return 0.0
    size = 1 << int(np.ceil(np.log2(la + lb)))
    corr = np.fft.irfft(np.fft.rfft(a.samples, size) * np.conj(np.fft.rfft(b.samples, size)),
                        size)
    lag = int(0.03 * RATE)
    window = np.concatenate([corr[:lag + 1], corr[-lag:]])
    denominator = np.linalg.norm(a.samples) * np.linalg.norm(b.samples)
    return float(window.max() / denominator) if denominator else 0.0


@dataclass
class SoundGroup:
    level: str  # exact | same | similar: the weakest link inside
    members: list[str]  # project sounds (paths under sound/)
    stock: list[str] = field(default_factory=list)  # matching stock sounds
    score: float = 1.0  # the weakest link's score
    keeper: str = ""  # the suggested one to keep (a stock sound when there is one)

    @property
    def saves(self) -> int:
        """Files the group stops needing if every member uses the keeper."""
        return len(self.members) - (0 if self.keeper in self.stock else 1)


def analyse(sounds: dict[str, Path], stock: dict[str, Path] | None = None, *,
            users: dict[str, int] | None = None,
            similar_threshold: float = SIMILAR_THRESHOLD,
            progress: Callable[[int, int, str], None] | None = None) -> list[SoundGroup]:
    """Groups of project ``sounds`` (name -> file) that could share one file,
    with the ``stock`` sounds (name -> file) they match. ``users``: how many
    models play each sound (the most used is the suggested keeper)."""
    stock = stock or {}
    users = users or {}
    prints: list[Fingerprint] = []
    items = [(n, p, False) for n, p in sounds.items()] + [(n, p, True) for n, p in stock.items()]
    for done, (name, path, is_stock) in enumerate(items):
        if progress is not None:
            progress(done, len(items), name)
        try:
            found = fingerprint(path, name, stock=is_stock)
        except OSError:
            found = None
        if found is not None:
            prints.append(found)
    order = sorted(range(len(prints)), key=lambda k: prints[k].duration)

    def pairs():
        """Candidate pairs, shortest first (lengths within 1.6x)."""
        for position, i in enumerate(order):
            for j in order[position + 1:]:
                if prints[j].duration > prints[i].duration * 1.6 + 0.05:
                    break  # sorted by length: nothing further can be close
                if not (prints[i].stock and prints[j].stock):  # stock vs stock: not ours
                    yield i, j

    # pass 1: exact copies and the same sound saved another way
    safe = _Union(len(prints))
    rough: dict[tuple[int, int], float] = {}
    for i, j in pairs():
        a, b = prints[i], prints[j]
        if a.md5 == b.md5:
            safe.join(i, j, 0, 1.0)
            continue
        score = similarity(a, b)
        if role(a.name) and role(b.name) and role(a.name) != role(b.name):
            score = min(score, 0.5)  # different jobs: at most "not similar"
        rough[(i, j)] = score
        if score >= 0.85:
            same = same_sound(a, b)
            if same >= SAME_THRESHOLD:
                safe.join(i, j, 1, same)
    groups: list[SoundGroup | None] = []
    leader: dict[int, int] = {}  # a sound -> its safe group's keeper
    weight: dict[int, int] = {}  # a keeper -> how many sounds it stands for
    index = {p.name: k for k, p in enumerate(prints)}
    for members, level, score in safe.groups():
        group = _group(prints, members, level, score, users)
        groups.append(group)
        if group is not None:
            keeper = index[group.keeper]
            weight[keeper] = len(members)
            for i in members:
                leader[i] = keeper
    # pass 2: similar recordings, between what pass 1 left as one sound each
    # (a safe group takes part through its keeper, so the two never disagree)
    similar = _Union(len(prints))
    for (i, j), score in rough.items():
        li, lj = leader.get(i, i), leader.get(j, j)
        if li != lj and score >= similar_threshold:
            similar.join(li, lj, 2, score)
    for members, _level, score in similar.groups():
        groups.append(_group(prints, members, 2, score, users, weight))
    groups = [g for g in groups if g is not None]
    return sorted(groups, key=lambda g: (LEVELS.index(g.level), -g.saves, g.keeper.lower()))


class _Union:
    """Union-find remembering each set's weakest link (level, score)."""

    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.link: dict[int, tuple[int, float]] = {}

    def root(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def join(self, i: int, j: int, level: int, score: float) -> None:
        ri, rj = self.root(i), self.root(j)
        levels = [level] + [self.link[r][0] for r in (ri, rj) if r in self.link]
        scores = [score] + [self.link[r][1] for r in (ri, rj) if r in self.link]
        self.parent[ri] = rj
        self.link[rj] = (max(levels), min(scores))

    def groups(self) -> list[tuple[list[int], int, float]]:
        sets: dict[int, list[int]] = {}
        for i in range(len(self.parent)):
            sets.setdefault(self.root(i), []).append(i)
        return [(members, *self.link.get(key, (0, 1.0))) for key, members in sets.items()
                if len(members) > 1]


def _group(prints: list[Fingerprint], members: list[int], level: int, score: float,
           users: dict[str, int], weight: dict[int, int] | None = None
           ) -> SoundGroup | None:
    own = sorted((prints[i].name for i in members if not prints[i].stock), key=str.lower)
    stock_names = sorted(prints[i].name for i in members if prints[i].stock)
    if not own or len(own) + len(stock_names) < 2:
        return None
    weight = weight or {}
    by_name = {prints[i].name: weight.get(i, 1) for i in members}
    # a stock sound; else the one most models play, standing for most copies
    keeper = stock_names[0] if stock_names else max(
        own, key=lambda n: (users.get(n.lower(), 0), by_name.get(n, 1), -len(n), n))
    return SoundGroup(LEVELS[level], own, stock_names, round(score, 3), keeper)


__all__ = ["Fingerprint", "LEVELS", "role", "SAME_THRESHOLD", "SIMILAR_THRESHOLD", "SoundGroup",
           "analyse", "fingerprint", "same_sound", "similarity"]
