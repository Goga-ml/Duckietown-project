"""Standalone validation for the traffic-sign perception pipeline.

Lets Person A verify the detector end-to-end without the Duckiebot or the
simulator (the Godot sim has no AprilTags). Run from the project root:

    # synthetic self-test: render a known tag, detect it, check the signal
    python -m tasks.traffic_signs.packages.test_detector

    # run on a photo of a real sign (annotated copy saved next to it)
    python -m tasks.traffic_signs.packages.test_detector --image my_sign.jpg

    # live webcam preview (press q to quit)
    python -m tasks.traffic_signs.packages.test_detector --webcam
"""

import argparse
import os
import sys

import cv2
import numpy as np

# Allow running as a script from the project root.
_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tasks.traffic_signs.packages.agent import TrafficSignAgent
from tasks.traffic_signs.packages import detection_activity as activity
from tasks.traffic_signs.packages.sign_lookup import DEFAULT_TAG_IDS, build_lookup


def annotate(frame_rgb, detections, active):
    """Draw tag outlines + labels on a copy (BGR) for display/saving."""
    bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
    for d in detections:
        pts = d.corners.astype(int)
        colour = (0, 200, 0) if d.sign_type else (160, 160, 160)
        cv2.polylines(bgr, [pts], True, colour, 2)
        label = f"{d.sign_type or f'id{d.tag_id}'} {d.distance_m:.2f}m"
        cx, cy = int(d.center[0]), int(d.center[1])
        cv2.putText(bgr, label, (cx - 40, cy - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1, cv2.LINE_AA)
    if active:
        banner = f"ACTIVE: {active['sign_type']} @ {active['distance_m']}m at_sign={active['at_sign']}"
        cv2.putText(bgr, banner, (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2, cv2.LINE_AA)
    return bgr


def _report(detections, active):
    print(f"  detections: {len(detections)}")
    for d in detections:
        print(f"    tag {d.tag_id:>3} -> {d.sign_type or '(unknown)':<18} "
              f"dist={d.distance_m:.2f}m size={d.pixel_size:.0f}px offset={d.offset_norm:+.2f}")
    print(f"  active sign: {active}")


def _render_tag(tag_id: int, tag_px: int, canvas=(640, 480), center=None):
    """Render one AprilTag (with quiet zone) onto a white RGB canvas."""
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    marker = cv2.aruco.generateImageMarker(dictionary, tag_id, tag_px)
    w, h = canvas
    img = np.full((h, w), 255, np.uint8)
    cx, cy = center or (w // 2, h // 2)
    x0, y0 = cx - tag_px // 2, cy - tag_px // 2
    img[y0:y0 + tag_px, x0:x0 + tag_px] = marker
    return cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)


def run_selftest() -> int:
    print("[self-test] Rendering a synthetic 'stop' tag and detecting it...")

    # Pick a tag id that maps to 'stop' in the default table.
    lookup = build_lookup(None)
    stop_id = next((tid for tid, s in DEFAULT_TAG_IDS.items() if s == "stop"), 25)

    agent = TrafficSignAgent()
    # 80px tag at fx=340, size=0.065m -> ~0.28m, i.e. inside AT_SIGN_DISTANCE_M.
    frame = _render_tag(stop_id, tag_px=80)

    detections = agent.detect(frame)
    active = activity.select_active_sign(detections or [])
    _report(detections or [], active)

    ok = True
    if not detections:
        print("  FAIL: tag was not detected"); ok = False
    else:
        d = detections[0]
        if d.tag_id != stop_id:
            print(f"  FAIL: detected id {d.tag_id}, expected {stop_id}"); ok = False
        if d.sign_type != "stop":
            print(f"  FAIL: sign_type {d.sign_type!r}, expected 'stop'"); ok = False
        if not (0.15 < d.distance_m < 0.45):
            print(f"  WARN: distance {d.distance_m:.2f}m outside expected ~0.28m "
                  f"(check camera intrinsics / tag size in config)")
    if not active or active.get("sign_type") != "stop":
        print("  FAIL: active sign was not 'stop'"); ok = False
    elif not active["at_sign"]:
        print("  WARN: at_sign is False (tag rendered just beyond AT_SIGN_DISTANCE_M)")

    print("[self-test] PASS" if ok else "[self-test] FAILED")
    return 0 if ok else 1


def run_image(path: str) -> int:
    frame_bgr = cv2.imread(path)
    if frame_bgr is None:
        print(f"Could not read image: {path}")
        return 1
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    agent = TrafficSignAgent()
    detections = agent.detect(frame_rgb) or []
    active = activity.select_active_sign(detections)
    print(f"[image] {path}")
    _report(detections, active)

    out = annotate(frame_rgb, detections, active)
    root, ext = os.path.splitext(path)
    out_path = f"{root}_annotated{ext or '.jpg'}"
    cv2.imwrite(out_path, out)
    print(f"  annotated copy: {out_path}")
    return 0


def run_webcam(cam_index: int) -> int:
    cap = cv2.VideoCapture(cam_index)
    if not cap.isOpened():
        print(f"Could not open webcam {cam_index}")
        return 1
    agent = TrafficSignAgent()
    print("[webcam] press q to quit")
    try:
        while True:
            ok, frame_bgr = cap.read()
            if not ok:
                break
            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            detections = agent.detect(frame_rgb) or []
            active = activity.select_active_sign(detections)
            cv2.imshow("traffic-sign detector", annotate(frame_rgb, detections, active))
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Traffic-sign perception tester")
    ap.add_argument('--image', type=str, help="run on an image file")
    ap.add_argument('--webcam', action='store_true', help="run on a live webcam")
    ap.add_argument('--cam-index', type=int, default=0)
    args = ap.parse_args()

    if args.image:
        return run_image(args.image)
    if args.webcam:
        return run_webcam(args.cam_index)
    return run_selftest()


if __name__ == '__main__':
    sys.exit(main())
