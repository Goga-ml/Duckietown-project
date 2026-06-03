import cv2
import numpy as np

from tasks.traffic_signs.packages.sign_lookup import (
    CAT_REGULATORY, CAT_INTERSECTION, CAT_INFO, SIGN_INFO,
)

# Colour per sign category (BGR).
_CAT_COLORS = {
    CAT_REGULATORY:   (60, 60, 220),    # red-ish: stop / yield
    CAT_INTERSECTION: (60, 200, 60),    # green: turn-option signs
    CAT_INFO:         (220, 170, 50),   # blue-ish: informational
}
_UNKNOWN_COLOR = (160, 160, 160)


def _color_for(det):
    meta = SIGN_INFO.get(det.sign_type) if det.sign_type else None
    return _CAT_COLORS.get(meta["category"], _UNKNOWN_COLOR) if meta else _UNKNOWN_COLOR


def draw_signs(image_bgr: np.ndarray, detections: list) -> np.ndarray:
    """Draw each detected tag's outline + sign label + distance."""
    out = image_bgr
    for det in detections:
        color = _color_for(det)
        pts = det.corners.astype(np.int32)
        cv2.polylines(out, [pts], isClosed=True, color=color, thickness=2)

        name = det.sign_type or f"id {det.tag_id}"
        label = f"{name}  {det.distance_m:.2f}m"
        x1, y1, _x2, _y2 = det.bbox

        (tw, th), baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        ly = max(0, y1 - th - baseline - 4)
        cv2.rectangle(out, (x1, ly), (x1 + tw + 6, ly + th + baseline + 4), color, -1)
        cv2.putText(out, label, (x1 + 3, ly + th + 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)
    return out


def draw_active_banner(image_bgr: np.ndarray, active: dict) -> np.ndarray:
    """Top banner showing the single sign the behaviour layer should act on."""
    if not active:
        return image_bgr
    out = image_bgr
    turns = active.get("turns")
    turns_txt = f"  turns={','.join(turns)}" if turns else ""
    at = "  [AT SIGN]" if active.get("at_sign") else ""
    text = f"ACTIVE: {active['sign_type']}  {active['distance_m']}m{turns_txt}{at}"

    (tw, th), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
    cv2.rectangle(out, (8, 8), (8 + tw + 12, 8 + th + baseline + 10),
                  (0, 0, 0), -1)
    cv2.putText(out, text, (14, 8 + th + 4),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 220, 255), 2, cv2.LINE_AA)
    return out


# Colour per behaviour state (BGR), so the operator can read the state machine
# at a glance from the video feed.
_STATE_COLORS = {
    "DRIVING":                  (60, 200, 60),    # green: cruising
    "APPROACHING_SIGN":         (0, 200, 255),    # amber: slowing down
    "STOPPED":                  (60, 60, 220),    # red: stopped at a sign
    "WAITING_FOR_RIGHT_OF_WAY": (0, 140, 255),    # orange: giving way
    "TURNING":                  (220, 170, 50),   # blue: executing a turn
    "OBSTACLE_STOP":            (60, 60, 220),    # red: blocked
}


def draw_behavior_state(image_bgr: np.ndarray, state: str, note: str = "",
                        y: int = 44) -> np.ndarray:
    """Draw the behaviour state machine's current state + note under the active
    sign banner (top-left), colour-coded by state."""
    out = image_bgr
    text = f"STATE: {state}"
    if note:
        text += f"  |  {note}"
    color = _STATE_COLORS.get(state, (200, 200, 200))

    (tw, th), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
    cv2.rectangle(out, (8, y), (8 + tw + 12, y + th + baseline + 8), (0, 0, 0), -1)
    cv2.putText(out, text, (14, y + th + 4),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)
    return out


def draw_status_overlay(image_bgr: np.ndarray, message: str) -> np.ndarray:
    out = image_bgr.copy()
    pad = 10
    (tw, th), baseline = cv2.getTextSize(message, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
    cv2.rectangle(out, (pad, pad), (pad + tw + 12, pad + th + baseline + 8), (0, 0, 0), -1)
    cv2.putText(out, message, (pad + 6, pad + th + 4),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 1, cv2.LINE_AA)
    return out


def draw_lane_overlay(image_bgr: np.ndarray, lane_dbg: dict, tol: int = 5) -> np.ndarray:
    """Overlay the lane-follower's per-slice sample points so the operator can
    confirm the bot is actually tracking the lane markings.

    ``lane_dbg`` is LaneServoingAgent.last_debug_info: it carries the yellow/white
    lane masks and the y of each horizontal sampling slice. We recompute the mean
    x per slice from the masks (so each dot lines up with its slice) and draw a
    yellow dot on the inner line and a white dot on the outer line, plus a centre
    reference line and a small 'lane detected' status."""
    if not lane_dbg:
        return image_bgr
    out = image_bgr
    h, w = out.shape[:2]

    ym = lane_dbg.get('yellow_mask')
    wm = lane_dbg.get('white_mask')
    slice_ys = lane_dbg.get('slice_ys') or []

    # Centre reference (where the bot aims to keep the lane centre).
    cv2.line(out, (w // 2, int(h * 0.45)), (w // 2, h), (90, 90, 90), 1, cv2.LINE_AA)

    for y in slice_ys:
        if not (0 <= y < h):
            continue
        y0, y1 = max(0, y - tol), min(h, y + tol)
        for mask, color in ((ym, (0, 255, 255)), (wm, (255, 255, 255))):
            if mask is None or mask.shape[:2] != (h, w):
                continue
            xs = np.where(mask[y0:y1, :] > 0)[1]
            if len(xs):
                mx = int(xs.mean())
                cv2.circle(out, (mx, y), 6, color, -1)
                cv2.circle(out, (mx, y), 6, (0, 0, 0), 1)

    detected = bool(lane_dbg.get('lane_detected'))
    txt = "LANE: tracking" if detected else "LANE: searching"
    color = (60, 200, 60) if detected else (0, 170, 255)
    cv2.putText(out, txt, (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)
    return out
