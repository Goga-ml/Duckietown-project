"""Tunable perception logic for traffic-sign recognition (Person A).

This file is the seam between perception and behaviour:

  * the filter_* functions decide which raw AprilTag detections are trustworthy
    (drop tiny / far-away / spurious tags), and
  * select_active_sign() collapses the surviving detections into the single,
    clean signal Person B's state machine consumes:

        {
          "tag_id":      9,
          "sign_type":   "stop",
          "turns":       None | ["left", "right"],
          "distance_m":  0.30,
          "offset_norm": -0.12,   # -1 far-left .. +1 far-right in the frame
          "at_sign":     True,    # within AT_SIGN_DISTANCE_M of the bot
        }

Keep this contract stable — Person B codes against these keys.
"""

from typing import List, Optional

# Distance (metres) at which we consider the bot to have *arrived* at the sign,
# i.e. the point where Person B should act (stop, choose a turn, ...). Tune to
# where the camera can still see the tag just before the stop line.
AT_SIGN_DISTANCE_M = 0.30

# Only treat a sign as "ahead of us" if its horizontal position is within this
# fraction of half the frame width. Signs way off to the side belong to the
# cross-street, not our approach, so we ignore them for the active signal.
_MAX_ABS_OFFSET = 0.6

# Ignore detections claiming to be further than this — almost always a
# misread or a sign at the next intersection, not the one we are approaching.
_MAX_DISTANCE_M = 1.5

# Reject tags whose apparent size is below this many pixels. A real sign tag
# within ~1.5 m fills well over this; smaller blobs are noise.
_MIN_PIXEL_SIZE = 18.0


def NUMBER_FRAMES_SKIPPED() -> int:
    # 0 = run the detector on every frame. AprilTag detection is cheap, so we
    # keep full rate for the lowest reaction latency. Raise to 1 only if the
    # bot's CPU is saturated by the lane follower running alongside.
    return 0


def filter_by_size(pixel_size: float) -> bool:
    """Reject tags too small to be a real, close sign."""
    return pixel_size >= _MIN_PIXEL_SIZE


def filter_by_distance(distance_m: float) -> bool:
    """Reject implausible / out-of-range distance estimates."""
    return 0.0 < distance_m <= _MAX_DISTANCE_M


def select_active_sign(detections: List) -> Optional[dict]:
    """Pick the one sign the bot should act on, as a plain dict (or None).

    Rule: of the *known* signs roughly ahead of us, choose the nearest.
    """
    candidates = [
        d for d in detections
        if d.sign_type is not None and abs(d.offset_norm) <= _MAX_ABS_OFFSET
    ]
    if not candidates:
        return None

    nearest = min(candidates, key=lambda d: d.distance_m)
    return {
        "tag_id":      nearest.tag_id,
        "sign_type":   nearest.sign_type,
        "turns":       nearest.turns,
        "distance_m":  round(nearest.distance_m, 3),
        "offset_norm": round(nearest.offset_norm, 3),
        "at_sign":     nearest.distance_m <= AT_SIGN_DISTANCE_M,
    }
