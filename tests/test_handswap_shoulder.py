"""The retarget keeps our shoulder off screen by bending at the elbow."""

from __future__ import annotations

import numpy as np

from valve_qc_merger.handswap.math3d import normalized
from valve_qc_merger.handswap.retarget import (
    MAX_SHOULDER_BEND,
    _forearm_seen,
    _hidden_shoulder,
    _keep_forearm_hidden,
    _on_screen,
    _segment_seen,
    _shoulder_cap_seen,
)


def test_an_off_screen_shoulder_is_left_alone() -> None:
    elbow = np.array([6.0, -8.0, -8.0])
    d = normalized(np.array([0.2, 1.0, -0.6]))  # back and down: behind the eye
    shoulder = _hidden_shoulder(elbow, d, 10.0)
    assert np.allclose(shoulder, elbow + d * 10.0)


def test_a_shoulder_in_view_bends_out_of_it() -> None:
    # CSO draw: the forearm line runs sideways across the view, so the
    # shoulder straight on along it lands on screen
    elbow = np.array([8.0, -10.0, -6.0])
    d = normalized(np.array([1.0, -0.5, 0.3]))
    upper = 10.0
    assert _shoulder_cap_seen(elbow + d * upper, d)
    shoulder = _hidden_shoulder(elbow, d, upper)
    u = normalized(shoulder - elbow)
    assert np.isclose(np.linalg.norm(shoulder - elbow), upper)  # bones keep their length
    assert not _shoulder_cap_seen(shoulder, u)
    assert np.arccos(np.clip(np.dot(u, d), -1, 1)) <= MAX_SHOULDER_BEND + 1e-6


def test_an_arm_part_turns_no_faster_than_allowed() -> None:
    # frame-to-frame continuity: a 90 deg flip of the forearm is spread out
    from valve_qc_merger.handswap.retarget import _angle, _limit_turn
    previous = np.array([1.0, 0.0, 0.0])
    wanted = np.array([0.0, 1.0, 0.0])
    limited = _limit_turn(previous, wanted, 15.0)
    assert np.isclose(_angle(previous, limited), 15.0)
    assert np.isclose(np.linalg.norm(limited), 1.0)
    assert np.allclose(_limit_turn(previous, wanted, 120.0), wanted)  # within the limit


def test_an_arm_the_original_kept_out_of_view_stays_out() -> None:
    # v_c4's draw: the hands still below the screen; the natural forearm
    # pointed up into view, the original's down — turned back just enough
    wrist = np.array([4.0, -14.0, -16.0])
    assert not _on_screen(wrist)
    natural = normalized(np.array([0.3, 0.2, 1.0]))
    original = normalized(np.array([0.2, 0.6, -0.8]))
    assert _forearm_seen(wrist, natural, 12.0) and not _forearm_seen(wrist, original, 12.0)
    kept = _keep_forearm_hidden(wrist, natural, original, 12.0)
    assert not _forearm_seen(wrist, kept, 12.0)
    assert np.dot(kept, natural) > np.dot(original, natural)  # no further than it takes
    # the whole upper arm (not only its head) leaves the view then
    elbow = wrist + kept * 12.0
    shoulder = _hidden_shoulder(elbow, kept, 10.0, whole=True)
    assert not _segment_seen(elbow, normalized(shoulder - elbow), 10.0, (0.0, 0.5, 1.0))
