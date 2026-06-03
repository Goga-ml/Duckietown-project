"""AprilTag traffic-sign perception agent.

Person A's core deliverable. Given a camera frame it:
  1. detects AprilTags (Duckietown's tag36h11 family),
  2. estimates each tag's distance + lateral offset from the camera
     (solvePnP with the known physical tag size and camera intrinsics),
  3. resolves the tag ID to a sign meaning (see sign_lookup.py),
  4. applies the student-tunable filters in detection_activity.py to drop
     spurious / far-away detections.

The behaviour side (Person B) consumes the resulting SignDetection list and
the single "active sign" signal built in detection_activity.select_active_sign.

Detector backend: chosen at runtime so the same code runs on a dev machine and
on the Duckiebot, whose OpenCV builds differ. In preference order:
  1. cv2.aruco  (ArucoDetector, OpenCV >= 4.7 with contrib)
  2. cv2.aruco  (legacy Dictionary_get/detectMarkers, OpenCV 4.0-4.6 contrib)
  3. dt_apriltags / pupil_apriltags  (the libs Duckietown images ship)
  4. apriltag   (the pip 'apriltag' package)
All decode the same tag36h11 family, so the rest of the pipeline is identical.
If none is available the agent still imports and the server still serves; it
just reports load_error and returns no detections. Pose uses cv2.solvePnP,
which is in base OpenCV (no contrib needed).
"""

import os
import sys
import importlib
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


def _order_corners(corners: np.ndarray) -> np.ndarray:
    """Return the 4 corners in a canonical TL, TR, BR, BL order (pixel coords,
    y down). Makes pose/solvePnP independent of each backend's corner ordering."""
    pts = np.asarray(corners, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = pts[:, 1] - pts[:, 0]          # y - x
    return np.array([
        pts[np.argmin(s)],             # TL: smallest x+y
        pts[np.argmin(d)],             # TR: smallest y-x
        pts[np.argmax(s)],             # BR: largest x+y
        pts[np.argmax(d)],             # BL: largest y-x
    ], dtype=np.float32)


# ---------------------------------------------------------------------------
# Detector backends. Each exposes .name and .detect(gray) -> list[(tag_id,
# corners(4,2) float)]. _make_backend tries them in preference order.
# ---------------------------------------------------------------------------
class _ArucoBackend:
    """cv2.aruco, both the >=4.7 ArucoDetector API and the legacy 4.0-4.6 API."""
    _FAMILY_ATTR = {
        "tag36h11": "DICT_APRILTAG_36h11", "tag36h10": "DICT_APRILTAG_36h10",
        "tag25h9":  "DICT_APRILTAG_25h9",  "tag16h5":  "DICT_APRILTAG_16h5",
    }

    def __init__(self, family, det_cfg):
        aruco = cv2.aruco  # raises AttributeError if contrib/aruco absent
        dict_attr = self._FAMILY_ATTR.get(family, "DICT_APRILTAG_36h11")
        dict_id = getattr(aruco, dict_attr)

        if hasattr(aruco, "ArucoDetector"):           # OpenCV >= 4.7
            dictionary = aruco.getPredefinedDictionary(dict_id)
            params = aruco.DetectorParameters()
            self._apply_params(params, det_cfg)
            self._detector = aruco.ArucoDetector(dictionary, params)
            self._legacy = None
            self.name = "cv2.aruco (ArucoDetector)"
        else:                                          # OpenCV 4.0-4.6
            get_dict = getattr(aruco, "getPredefinedDictionary", None) or aruco.Dictionary_get
            self._legacy = (get_dict(dict_id), aruco.DetectorParameters_create())
            self._apply_params(self._legacy[1], det_cfg)
            self._detector = None
            self.name = "cv2.aruco (legacy)"

    @staticmethod
    def _apply_params(params, det_cfg):
        for attr, key, default in (
            ("aprilTagQuadDecimate",   "quad_decimate", 1.0),
            ("aprilTagQuadSigma",      "quad_sigma", 0.0),
            ("minMarkerPerimeterRate", "min_marker_perimeter_rate", 0.03),
        ):
            if hasattr(params, attr):
                setattr(params, attr, float(det_cfg.get(key, default)))

    def detect(self, gray):
        if self._detector is not None:
            corners_list, ids, _ = self._detector.detectMarkers(gray)
        else:
            corners_list, ids, _ = cv2.aruco.detectMarkers(
                gray, self._legacy[0], parameters=self._legacy[1])
        if ids is None or len(ids) == 0:
            return []
        return [(int(t), c.reshape(4, 2).astype(np.float32))
                for c, t in zip(corners_list, ids.ravel())]


class _ApriltagsLibBackend:
    """dt_apriltags or pupil_apriltags (same Detector API)."""
    def __init__(self, family, det_cfg):
        try:
            from dt_apriltags import Detector
            self.name = "dt_apriltags"
        except ImportError:
            from pupil_apriltags import Detector
            self.name = "pupil_apriltags"
        self._det = Detector(
            families=family,
            quad_decimate=float(det_cfg.get("quad_decimate", 1.0)),
            quad_sigma=float(det_cfg.get("quad_sigma", 0.0)),
        )

    def detect(self, gray):
        return [(int(d.tag_id), np.asarray(d.corners, dtype=np.float32))
                for d in self._det.detect(gray)]


class _ApriltagBackend:
    """The pip 'apriltag' package (different options API)."""
    def __init__(self, family, det_cfg):
        import apriltag
        self.name = "apriltag"
        options = apriltag.DetectorOptions(
            families=family,
            quad_decimate=float(det_cfg.get("quad_decimate", 1.0)),
        )
        self._det = apriltag.Detector(options)

    def detect(self, gray):
        return [(int(d.tag_id), np.asarray(d.corners, dtype=np.float32))
                for d in self._det.detect(gray)]


def _try_backends(family, det_cfg):
    """Try each backend in preference order. Returns (backend, tried_errors)."""
    tried = []
    for cls in (_ArucoBackend, _ApriltagsLibBackend, _ApriltagBackend):
        try:
            return cls(family, det_cfg), tried
        except Exception as e:
            tried.append(f"{cls.__name__}: {type(e).__name__}: {e}")
    return None, tried


_BOOTSTRAP_ATTEMPTED = False
_BOOTSTRAP_LOG = []          # human-readable trace, surfaced via /status load_error


def _bootstrap_install(timeout: int) -> bool:
    """Best-effort, one-time runtime install of an AprilTag backend.

    The Duckiebot ships plain opencv-python (no cv2.aruco) and none of the
    AprilTag libs, and we can't pre-bake them into the deploy package (they are
    compiled, arch-specific wheels). So when no backend is found we pip-install
    one here, once, into the user site. Best-effort: any failure (offline, no
    wheel) just leaves detection disabled — the server keeps running. Disable
    with `detector.auto_install: false` in the config.

    Every step is recorded in _BOOTSTRAP_LOG (and printed with flush=True) so the
    outcome is visible in /status even though the bot buffers stdout."""
    global _BOOTSTRAP_ATTEMPTED
    if _BOOTSTRAP_ATTEMPTED:
        return False
    _BOOTSTRAP_ATTEMPTED = True

    import subprocess
    import platform
    env = f"python={sys.version.split()[0]} machine={platform.machine()} pip via {sys.executable}"
    _BOOTSTRAP_LOG.append(env)
    print(f"[TrafficSigns] bootstrap: {env}", flush=True)

    # The Duckiebot's stock pip (Jetson, py3.6) predates the manylinux2014 tag,
    # so it can't see the aarch64 AprilTag wheels (dt-apriltags ships a
    # py3-none-manylinux2014_aarch64 wheel that works on any py3). Upgrade pip
    # first — best-effort — so that wheel resolves.
    try:
        up = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user", "-U", "pip"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout,
        )
        last = (up.stdout or up.stderr or "").strip().replace("\n", " ")[-120:]
        _BOOTSTRAP_LOG.append(f"pip self-upgrade: rc={up.returncode} {last}")
        print(f"[TrafficSigns] bootstrap: pip self-upgrade rc={up.returncode} {last}", flush=True)
    except Exception as e:
        _BOOTSTRAP_LOG.append(f"pip self-upgrade: {type(e).__name__}: {e}")

    for pkg in ("dt-apriltags", "pupil-apriltags"):
        for extra in (["--user"], []):     # --user first; fall back to plain
            tag = f"pip install {' '.join(extra + [pkg])}".strip()
            try:
                print(f"[TrafficSigns] bootstrap: trying `{tag}` ...", flush=True)
                # NB: capture_output= and text= are Python 3.7+; the Duckiebot
                # runs 3.6, so use the stdout/stderr + universal_newlines form.
                r = subprocess.run(
                    [sys.executable, "-m", "pip", "install", *extra, pkg],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    universal_newlines=True, timeout=timeout,
                )
                if r.returncode == 0:
                    _BOOTSTRAP_LOG.append(f"{tag}: OK")
                    print(f"[TrafficSigns] bootstrap: installed {pkg}.", flush=True)
                    # Make the freshly-installed package importable in-process.
                    try:
                        import site
                        usp = site.getusersitepackages()
                        if usp and usp not in sys.path:
                            sys.path.append(usp)
                    except Exception:
                        pass
                    importlib.invalidate_caches()
                    return True
                tail = (r.stderr or r.stdout or "").strip().replace("\n", " ")[-220:]
                _BOOTSTRAP_LOG.append(f"{tag}: rc={r.returncode} {tail}")
                print(f"[TrafficSigns] bootstrap: {tag} rc={r.returncode} {tail}", flush=True)
            except Exception as e:
                _BOOTSTRAP_LOG.append(f"{tag}: {type(e).__name__}: {e}")
                print(f"[TrafficSigns] bootstrap: {tag} {type(e).__name__}: {e}", flush=True)
    return False


def _make_backend(family, det_cfg):
    """Return (backend, None) for the first available backend, else (None, error).

    If nothing is available and `detector.auto_install` is on (default), try a
    one-time runtime install of a backend, then retry."""
    backend, tried = _try_backends(family, det_cfg)
    if backend is not None:
        return backend, None

    if bool(det_cfg.get('auto_install', True)):
        if _bootstrap_install(int(det_cfg.get('auto_install_timeout', 240))):
            backend, tried = _try_backends(family, det_cfg)
            if backend is not None:
                return backend, None

    err = ("No AprilTag backend available. Install one of: "
           "dt-apriltags, pupil-apriltags, apriltag, or "
           "opencv-contrib-python (cv2.aruco). Tried -> " + " | ".join(tried))
    if _BOOTSTRAP_LOG:
        err += "  ||  auto-install: " + " ;; ".join(_BOOTSTRAP_LOG)
    return None, err


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
        # to match _order_corners().
        s = self.tag_size / 2.0
        self._obj_pts = np.array([[-s,  s, 0],
                                  [ s,  s, 0],
                                  [ s, -s, 0],
                                  [-s, -s, 0]], dtype=np.float64)

        self.frame_count = 0
        self.lookup = build_lookup(cfg)
        self._backend, self.load_error = _make_backend(self.family, det_cfg)
        if self._backend is not None:
            print(f"[TrafficSigns] AprilTag detector ready "
                  f"(backend={self._backend.name}, family={self.family}, "
                  f"tag_size={self.tag_size} m).")
        else:
            print(f"[TrafficSigns] {self.load_error}")

    @property
    def backend_name(self) -> Optional[str]:
        return self._backend.name if self._backend else None

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
            # IPPE_SQUARE is the right solver for one square marker, but isn't in
            # every OpenCV build — fall back to the default iterative solver.
            flag = getattr(cv2, 'SOLVEPNP_IPPE_SQUARE', 0)
            ok, _rvec, tvec = cv2.solvePnP(
                self._obj_pts, corners.astype(np.float64),
                self.K, self.dist, flags=flag,
            )
            if ok:
                return float(tvec[2][0]), float(tvec[0][0])
        except Exception:
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

        if self._backend is None:
            return []

        skip = self._frame_skip()
        if skip > 0 and (self.frame_count % (skip + 1)) != 0:
            return None

        h, w = frame_rgb.shape[:2]
        gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)

        try:
            raw = self._backend.detect(gray)
        except Exception as e:
            print(f"[TrafficSigns] detect error: {e}")
            return None

        if not raw:
            return []

        detections: List[SignDetection] = []
        for tag_id, raw_corners in raw:
            corners = _order_corners(raw_corners)
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
