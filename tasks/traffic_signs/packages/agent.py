"""AprilTag traffic-sign perception agent.

Person A's core deliverable. Given a camera frame it:
  1. detects AprilTags (Duckietown's tag36h11 family) via cv2.aruco,
  2. estimates each tag's distance + lateral offset from the camera
     (solvePnP with the known physical tag size and camera intrinsics),
  3. resolves the tag ID to a sign meaning (see sign_lookup.py),
  4. applies the student-tunable filters in detection_activity.py to drop
     spurious / far-away detections.

The behaviour side (Person B) consumes the resulting SignDetection list and
the single "active sign" signal built in detection_activity.select_active_sign.

No model file and no extra dependencies: cv2.aruco's DICT_APRILTAG_36h11
decodes the exact family Duckietown signs are printed in.
"""

import os
import warnings
from dataclasses import dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np
import yaml

warnings.filterwarnings("ignore", category=FutureWarning)

from tasks.traffic_signs.packages import detection_activity as student
from tasks.traffic_signs.packages.sign_lookup import build_lookup

_CONFIG_FILE = os.path.normpath(os.path.join(
    os.path.dirname(__file__), '..', '..', '..', 'config', 'traffic_signs_config.yaml'
))

# cv2.aruco predefined-dictionary name for each supported family.
_FAMILY_DICTS = {
    "tag36h11": cv2.aruco.DICT_APRILTAG_36h11,
    "tag36h10": cv2.aruco.DICT_APRILTAG_36h10,
    "tag25h9":  cv2.aruco.DICT_APRILTAG_25h9,
    "tag16h5":  cv2.aruco.DICT_APRILTAG_16h5,
}


@dataclass
class SignDetection:
    """One detected traffic-sign AprilTag in a frame."""
    tag_id:      int
    sign_type:   Optional[str]          # None if the tag is not a known sign
    turns:       Optional[List[str]]    # allowed turns for intersection signs
    corners:     np.ndarray             # (4,2) float, order TL,TR,BR,BL (pixels)
    center:      Tuple[float, float]    # tag centre in pixels
    bbox:        Tuple[int, int, int, int]  # axis-aligned x1,y1,x2,y2 (pixels)
    pixel_size:  float                  # mean edge length in pixels
    distance_m:  float                  # forward distance (camera Z), metres
    offset_norm: float                  # lateral position in frame, -1 (left) .. +1 (right)


class TrafficSignAgent:

    def __init__(self, config_path: str = None):
        path = config_path or _CONFIG_FILE
        try:
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}

        det_cfg = cfg.get('detector', {}) or {}
        cam_cfg = cfg.get('camera', {}) or {}
        tag_cfg = cfg.get('tag', {}) or {}

        self.family   = det_cfg.get('family', 'tag36h11')
        self.tag_size = float(tag_cfg.get('size_m', 0.065))

        # Camera intrinsics for distance/pose.
        fx = float(cam_cfg.get('fx', 340.0))
        fy = float(cam_cfg.get('fy', 340.0))
        cx = float(cam_cfg.get('cx', 320.0))
        cy = float(cam_cfg.get('cy', 240.0))
        self.K = np.array([[fx, 0, cx],
                           [0, fy, cy],
                           [0,  0,  1]], dtype=np.float64)
        self.dist = np.array(cam_cfg.get('dist_coeffs', [0, 0, 0, 0, 0]),
                             dtype=np.float64).reshape(-1, 1)
        self._fx = fx

        # Tag corner model in the tag's own frame (metres), order TL,TR,BR,BL
        # to match cv2.aruco's corner output.
        s = self.tag_size / 2.0
        self._obj_pts = np.array([[-s,  s, 0],
                                  [ s,  s, 0],
                                  [ s, -s, 0],
                                  [-s, -s, 0]], dtype=np.float64)

        self.frame_count = 0
        self.lookup = build_lookup(cfg)
        self._detector = self._build_detector(det_cfg)
        self.load_error = None

    def _build_detector(self, det_cfg: dict):
        dict_id = _FAMILY_DICTS.get(self.family)
        if dict_id is None:
            self.load_error = (f"Unknown tag family {self.family!r}; "
                               f"supported: {sorted(_FAMILY_DICTS)}")
            print(f"[TrafficSigns] {self.load_error}")
            dict_id = cv2.aruco.DICT_APRILTAG_36h11

        dictionary = cv2.aruco.getPredefinedDictionary(dict_id)
        params = cv2.aruco.DetectorParameters()
        params.aprilTagQuadDecimate    = float(det_cfg.get('quad_decimate', 1.0))
        params.aprilTagQuadSigma       = float(det_cfg.get('quad_sigma', 0.0))
        params.minMarkerPerimeterRate  = float(det_cfg.get('min_marker_perimeter_rate', 0.03))
        print(f"[TrafficSigns] AprilTag detector ready "
              f"(family={self.family}, tag_size={self.tag_size} m).")
        return cv2.aruco.ArucoDetector(dictionary, params)

    def _frame_skip(self) -> int:
        try:
            return max(0, int(student.NUMBER_FRAMES_SKIPPED()))
        except Exception:
            return 0

    def _estimate_distance(self, corners: np.ndarray, pixel_size: float) -> Tuple[float, float]:
        """Return (forward_distance_m, lateral_offset_m) for one tag.

        Uses solvePnP (IPPE_SQUARE — the correct solver for a single square
        planar marker). Falls back to the pinhole apparent-size relation
        d = size * fx / pixel_width if solvePnP fails.
        """
        try:
            ok, _rvec, tvec = cv2.solvePnP(
                self._obj_pts, corners.astype(np.float64),
                self.K, self.dist, flags=cv2.SOLVEPNP_IPPE_SQUARE,
            )
            if ok:
                return float(tvec[2][0]), float(tvec[0][0])
        except cv2.error:
            pass
        # Fallback: monotonic in true distance, good enough for near/far gating.
        if pixel_size > 1e-3:
            return self.tag_size * self._fx / pixel_size, 0.0
        return float('inf'), 0.0

    def detect(self, frame_rgb: np.ndarray) -> Optional[List[SignDetection]]:
        """Detect sign tags in an RGB frame.

        Returns a list of SignDetection, or None on a skipped frame (so the
        caller keeps the previous result instead of clearing it).
        """
        self.frame_count += 1

        skip = self._frame_skip()
        if skip > 0 and (self.frame_count % (skip + 1)) != 0:
            return None

        h, w = frame_rgb.shape[:2]
        gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)

        try:
            corners_list, ids, _ = self._detector.detectMarkers(gray)
        except cv2.error as e:
            print(f"[TrafficSigns] detectMarkers error: {e}")
            return None

        if ids is None or len(ids) == 0:
            return []

        detections: List[SignDetection] = []
        for tag_corners, tag_id in zip(corners_list, ids.ravel()):
            corners = tag_corners.reshape(4, 2).astype(np.float32)
            cx = float(corners[:, 0].mean())
            cy = float(corners[:, 1].mean())

            # Mean of the four edge lengths = robust apparent size.
            edges = [np.linalg.norm(corners[i] - corners[(i + 1) % 4]) for i in range(4)]
            pixel_size = float(np.mean(edges))

            x1, y1 = corners.min(axis=0)
            x2, y2 = corners.max(axis=0)
            bbox = (int(x1), int(y1), int(x2), int(y2))

            distance_m, _lateral_m = self._estimate_distance(corners, pixel_size)
            offset_norm = float(np.clip((cx - w / 2.0) / (w / 2.0), -1.0, 1.0))

            tag_id = int(tag_id)
            sign_type = self.lookup.sign_type(tag_id)
            turns     = self.lookup.turns(tag_id)

            det = SignDetection(
                tag_id=tag_id, sign_type=sign_type, turns=turns,
                corners=corners, center=(cx, cy), bbox=bbox,
                pixel_size=pixel_size, distance_m=distance_m,
                offset_norm=offset_norm,
            )

            # Student-tunable rejection of spurious / out-of-range tags.
            if not student.filter_by_size(pixel_size):
                continue
            if not student.filter_by_distance(distance_m):
                continue

            detections.append(det)

        if self.frame_count % 60 == 0 and detections:
            nearest = min(detections, key=lambda d: d.distance_m)
            print(f"[TrafficSigns] frame={self.frame_count} "
                  f"#tags={len(detections)} nearest={nearest.sign_type or nearest.tag_id} "
                  f"@ {nearest.distance_m:.2f} m")

        return detections
