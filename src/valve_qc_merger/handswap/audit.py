"""Check a retarget against its original, frame by frame (no GPU).

The retarget's own verification proves the weapon trajectory and the grip
at the setup frame; this looks at EVERY frame of every sequence the way a
player would and measures what went wrong in practice:

- **weapon** — how far the kept weapon geometry strays from the original
  weapon (it must not move at all: a piece left on a deleted arm bone flew
  21 u away);
- **fit** — how far our hand's skin lies from the original hand's surface
  (mean of the grip vertices; arms excluded — ours are longer by design);
- **jumps** — frames where an arm bone of ours suddenly moves much more than
  in the frames around it while the original wrist does not (an elbow
  flipping, a shoulder hopping);
- **shoulder** — frames whose upper-arm head is in the player's view.

The view: SMD space with the eye at the origin, forward -Y, up +Z (studiomdl
turns SMD data +90 deg about Z), a widescreen Hor+ field of ~112 x 82 deg.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

VIEW_NEAR = 4.0
VIEW_TAN_H = math.tan(math.radians(56.0))
VIEW_TAN_V = math.tan(math.radians(41.0))
FIT_SAMPLES = 120  # grip vertices measured a frame
WEAPON_SAMPLES = 60
MEASURE_STEP = 3  # weapon and fit every 3rd frame; jumps and shoulder every frame

# verdict thresholds (units)
WEAPON_BAD = 0.05
FIT_WARN = 0.45
JUMP_FACTOR, JUMP_MARGIN = 2.5, 2.0  # a spike: > factor x neighbours + margin


@dataclass
class SequenceAudit:
    name: str
    frames: int
    weapon: float = 0.0  # worst weapon drift
    fit: float = 0.0  # mean grip-skin distance to the original hand
    fit95: float = 0.0  # worst frame's 95th percentile
    jumps: list[int] = field(default_factory=list)  # frames
    shoulder: int = 0  # frames with our shoulder in view


@dataclass
class RetargetAudit:
    weapon: float = 0.0
    fit: float = 0.0
    jumps: int = 0
    shoulder: int = 0
    frames: int = 0
    sequences: list[SequenceAudit] = field(default_factory=list)
    note: str = ""  # why nothing could be measured

    @property
    def problems(self) -> list[str]:
        """What a user should look at (empty: fine)."""
        out = []
        if self.weapon > WEAPON_BAD:
            worst = max(self.sequences, key=lambda s: s.weapon)
            out.append(f"weapon moved {self.weapon:.1f} u from the original ({worst.name})")
        if self.jumps:
            worst = max(self.sequences, key=lambda s: len(s.jumps))
            out.append(f"arms jump in {self.jumps} frame(s) ({worst.name} "
                       f"frame {worst.jumps[0]})")
        return out

    @property
    def warnings(self) -> list[str]:
        out = []
        if self.fit > FIT_WARN:
            out.append(f"hands sit {self.fit:.2f} u off the original's on average")
        if self.shoulder:
            worst = max(self.sequences, key=lambda s: s.shoulder)
            out.append(f"shoulder in view in {self.shoulder} of {self.frames} frame(s) "
                       f"({worst.name})")
        return out

    def summary(self) -> dict[str, object]:
        """What a project keeps (no per-sequence detail)."""
        return {"weapon": round(self.weapon, 3), "fit": round(self.fit, 3),
                "jumps": self.jumps, "shoulder": self.shoulder, "frames": self.frames,
                "problems": self.problems, "warnings": self.warnings,
                **({"note": self.note} if self.note else {})}

    def to_dict(self) -> dict[str, object]:
        return {**self.summary(), "sequences": [asdict(s) for s in self.sequences]}


def on_screen(points: np.ndarray) -> np.ndarray:
    """(N,) — which of (N,3) SMD-space points the player sees."""
    depth = -points[:, 1]
    return ((depth > VIEW_NEAR) & (np.abs(points[:, 0]) < depth * VIEW_TAN_H)
            & (np.abs(points[:, 2]) < depth * VIEW_TAN_V))


def _hands_stems(directory: Path) -> tuple[set[str], str | None]:
    """The QC's hands-group studios, and the (male) one to compare with."""
    qcs = sorted(directory.glob("*.qc"))
    if not qcs:
        return set(), None
    text = qcs[0].read_text(encoding="latin-1")
    match = re.search(r'\$bodygroup\s+"?[^"\n]*hand[^"\n]*"?\s*\{([^}]*)\}', text, re.I)
    stems = [Path(s.replace("\\", "/")).name
             for s in re.findall(r'studio\s+"([^"]+)"', match.group(1))] if match else []
    male = [s for s in stems if "female" not in s.lower()] or stems
    return set(stems), (male[0] if male else None)


def _spikes(speed: list[float], reference: list[float]) -> list[int]:
    """Indices where ``speed`` spikes over its neighbours while ``reference``
    (the original wrist's speed) does not."""
    out = []
    for i in range(1, len(speed) - 1):
        around = max(speed[i - 1], speed[i + 1])
        ref_around = max(reference[i - 1], reference[i + 1])
        if (speed[i] > JUMP_FACTOR * around + JUMP_MARGIN
                and reference[i] <= JUMP_FACTOR * ref_around + 0.5):
            out.append(i)
    return out


def audit_retarget(original: Path, result: Path) -> RetargetAudit:
    """Compare the decompiled ``original`` with its retarget ``result``."""
    from valve_qc_merger.handswap.surface import points_to_mesh
    from valve_qc_merger.studio.scene import build_scene
    audit = RetargetAudit()
    o, r = build_scene(Path(original)), build_scene(Path(result))
    all_hands, male = _hands_stems(Path(original))
    o_hand = [b for b in o.batches if b.stem == male]
    r_hand = [b for b in r.batches if b.stem == "hands"]
    if not o_hand or not r_hand:
        audit.note = "no hands group to compare with"
        return audit
    o_weapon = [b for b in o.batches if b.stem not in all_hands and len(b.bones)]
    r_weapon = [b for b in r.batches if b.stem != "hands" and len(b.bones)]
    arm = {i for i, n in enumerate(r.bone_names)
           if n.startswith(("UpperArm", "Arm0", "Arm1"))}
    tracked = [i for i, n in enumerate(r.bone_names)
               if n.startswith(("UpperArm", "Arm0", "Arm1", "Hand."))]
    upper = [i for i, n in enumerate(r.bone_names) if n.startswith("UpperArm")]
    rng = np.random.default_rng(0)
    bones = np.concatenate([b.bones for b in r_hand])
    grip = np.flatnonzero(~np.isin(bones, list(arm)))
    pick = rng.choice(grip, min(FIT_SAMPLES, len(grip)), replace=False)
    r_index = {s.name: i for i, s in enumerate(r.sequences)}
    fits: list[float] = []
    for si, seq in enumerate(o.sequences):
        ri = r_index.get(seq.name)
        if ri is None or r.sequences[ri].frames != seq.frames:
            continue
        sa = SequenceAudit(seq.name, seq.frames)
        speeds: list[float] = []
        wrist_speeds: list[float] = []
        prev = prev_wrist = None
        seq_fit: list[float] = []
        for f in range(seq.frames):
            ro, to = o.world(*o.local_pose(si, f))
            rr, tr = r.world(*r.local_pose(ri, f))
            measure = f % MEASURE_STEP == 0 or f == seq.frames - 1
            if measure and o_weapon and r_weapon:
                ow = np.concatenate([o.skin(b, ro, to)[0] for b in o_weapon])
                rw = np.concatenate([r.skin(b, rr, tr)[0] for b in r_weapon])
                sample = rw[rng.choice(len(rw), min(WEAPON_SAMPLES, len(rw)), replace=False)]
                gap = np.sqrt(((sample[:, None, :] - ow[None, :, :]) ** 2).sum(-1)).min(1)
                sa.weapon = max(sa.weapon, float(gap.max()))
            if measure:
                oh = np.concatenate([o.skin(b, ro, to)[0] for b in o_hand])
                rh = np.concatenate([r.skin(b, rr, tr)[0] for b in r_hand])
                err = points_to_mesh(rh[pick], oh.reshape(-1, 3, 3))
                seq_fit.append(float(err.mean()))
                sa.fit95 = max(sa.fit95, float(np.percentile(err, 95)))
            joints = tr[tracked]
            # the original hands' own motion (their skin: no guessing which
            # bone is a wrist — weapon bones are called "handle" too)
            wrists = np.concatenate([o.skin(b, ro, to)[0] for b in o_hand])
            if prev is not None:
                speeds.append(float(np.linalg.norm(joints - prev, axis=1).max()))
                wrist_speeds.append(float(np.linalg.norm(wrists - prev_wrist, axis=1).max()))
            prev, prev_wrist = joints, wrists
            if upper and on_screen(tr[upper]).any():
                sa.shoulder += 1
        sa.fit = float(np.mean(seq_fit)) if seq_fit else 0.0
        sa.jumps = [i + 1 for i in _spikes(speeds, wrist_speeds)]
        fits.append(sa.fit)
        audit.sequences.append(sa)
    if not audit.sequences:
        audit.note = "no sequence of the original found in the result"
        return audit
    audit.weapon = max(s.weapon for s in audit.sequences)
    audit.fit = float(np.mean(fits))
    audit.jumps = sum(len(s.jumps) for s in audit.sequences)
    audit.shoulder = sum(s.shoulder for s in audit.sequences)
    audit.frames = sum(s.frames for s in audit.sequences)
    return audit


__all__ = ["RetargetAudit", "SequenceAudit", "audit_retarget", "on_screen"]
