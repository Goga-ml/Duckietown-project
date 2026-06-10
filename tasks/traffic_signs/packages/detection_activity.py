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
          "pixel_size":  36.0,    # apparent tag edge length in px (closeness, calibration-free)
          "at_sign":     True,    # within AT_SIGN_DISTANCE_M of the bot
        }

Keep this contract stable — Person B codes against these keys.
"""

import os
from typing import List, Optional

import yaml

from tasks.traffic_signs.packages.sign_lookup import SIGN_INFO

_CONFIG_FILE = os.path.normpath(os.path.join(
    os.path.dirname(__file__), '..', '..', '..', 'config', 'traffic_signs_config.yaml'
))

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


def _load_unknown_sign_as() -> str:
    """Read `unknown_sign_as` from the task config ("" = disabled).

    When set (e.g. "yield"), a tag that decodes fine but is NOT in the tag-id ->
    sign mapping (config `signs:` block / sign_lookup defaults) is surfaced as
    that sign type instead of being dropped — but only when no *known* sign is
    in view (see select_active_sign). Validated against SIGN_INFO so a typo
    can't leak a vocabulary string the state machine doesn't understand.
    """
    try:
        with open(_CONFIG_FILE) as f:
            value = str((yaml.safe_load(f) or {}).get('unknown_sign_as', '') or '')
    except Exception:
        return ''
    if value and value not in SIGN_INFO:
        print(f"[Detection] unknown_sign_as='{value}' is not a known sign type; ignoring.")
        return ''
    if value:
        print(f"[Detection] unmapped tags will be treated as '{value}' "
              "(unknown_sign_as in config/traffic_signs_config.yaml).")
    return value


UNKNOWN_SIGN_AS = _load_unknown_sign_as()


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

    Rule: of the *known* signs roughly ahead of us, choose the nearest. Known
    signs always win; only when none is in view and UNKNOWN_SIGN_AS is set do
    we fall back to the nearest *unmapped* tag, surfaced as that sign type
    (it already passed the size/distance filters, so it's a plausible, close
    sign — on our map the only unmapped sign is the yield).
    """
    ahead = [d for d in detections if abs(d.offset_norm) <= _MAX_ABS_OFFSET]
    known = [d for d in ahead if d.sign_type is not None]

    if known:
        nearest = min(known, key=lambda d: d.distance_m)
        sign_type, turns = nearest.sign_type, nearest.turns
    elif UNKNOWN_SIGN_AS:
        unknown = [d for d in ahead if d.sign_type is None]
        if not unknown:
            return None
        nearest = min(unknown, key=lambda d: d.distance_m)
        sign_type, turns = UNKNOWN_SIGN_AS, SIGN_INFO[UNKNOWN_SIGN_AS]["turns"]
    else:
        return None

    return {
        "tag_id":      nearest.tag_id,
        "sign_type":   sign_type,
        "turns":       turns,
        "distance_m":  round(nearest.distance_m, 3),
        "offset_norm": round(nearest.offset_norm, 3),
        # Apparent tag edge length in pixels. Measured straight from the detected
        # corners, so — unlike distance_m — it does NOT depend on the camera
        # intrinsics or the assumed tag size being correct. The behaviour layer
        # uses it as a robust "how close am I?" signal that still works when the
        # configured intrinsics don't match the (sim or real) camera.
        "pixel_size":  round(nearest.pixel_size, 1),
        "at_sign":     nearest.distance_m <= AT_SIGN_DISTANCE_M,
    }
