"""Red stop-line detection.

A Duckietown intersection is marked by a red line painted across the lane. Signs
(stop / yield / turn-option) are mounted *before* the intersection, so the robot
must not act the moment it reaches the sign — it should keep lane-following until
it arrives at the red line, then stop / turn there (like real traffic).

This detector answers one question for the behaviour layer: "is the red stop
line right in front of me now?" It looks for a band of red pixels in a region
near the bottom of the camera frame; as the bot rolls up to the line that band
grows, and once it covers enough of the region we report the line as reached.

Tunables live in config/traffic_signs_config.yaml under `stop_line:` (all
optional — sensible defaults here). Red wraps around the hue circle, so two
HSV ranges are unioned.
"""

import os

import cv2
import numpy as np
import yaml

_CONFIG_FILE = os.path.normpath(os.path.join(
    os.path.dirname(__file__), '..', '..', '..', 'config', 'traffic_signs_config.yaml'
))


class StopLineDetector:
    def __init__(self, config: dict = None):
        if config is None:
            try:
                with open(_CONFIG_FILE) as f:
                    config = (yaml.safe_load(f) or {}).get('stop_line', {}) or {}
            except Exception:
                config = {}

        # ROI: a band near the bottom-centre of the frame (fractions of W/H).
        self.roi_top = float(config.get('roi_top', 0.65))   # ROI spans this..1.0 of height
        self.roi_x0  = float(config.get('roi_x0', 0.15))
        self.roi_x1  = float(config.get('roi_x1', 0.85))

        # Red HSV thresholds (OpenCV H 0-179). Red sits at both ends of the hue
        # circle, so we union [0..h_lo] and [h_hi..179].
        self.h_lo  = int(config.get('h_lo', 10))
        self.h_hi  = int(config.get('h_hi', 170))
        self.s_min = int(config.get('s_min', 90))
        self.v_min = int(config.get('v_min', 70))

        # The line is "reached" once red fills at least this fraction of the ROI.
        self.min_area_ratio = float(config.get('min_area_ratio', 0.06))

        self.last_ratio = 0.0   # exposed for tuning / overlays

    def detect(self, frame_rgb: np.ndarray) -> bool:
        """Return True when the red stop line is close enough to act on."""
        h, w = frame_rgb.shape[:2]
        y0 = int(h * self.roi_top)
        x0 = int(w * self.roi_x0)
        x1 = int(w * self.roi_x1)
        roi = frame_rgb[y0:h, x0:x1]
        if roi.size == 0:
            self.last_ratio = 0.0
            return False

        hsv = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)
        m1 = cv2.inRange(hsv, (0, self.s_min, self.v_min), (self.h_lo, 255, 255))
        m2 = cv2.inRange(hsv, (self.h_hi, self.s_min, self.v_min), (179, 255, 255))
        mask = cv2.bitwise_or(m1, m2)

        self.last_ratio = float(np.count_nonzero(mask)) / float(mask.size)
        return self.last_ratio >= self.min_area_ratio
