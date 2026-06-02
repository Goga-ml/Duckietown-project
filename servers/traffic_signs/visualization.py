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


def draw_status_overlay(image_bgr: np.ndarray, message: str) -> np.ndarray:
    out = image_bgr.copy()
    pad = 10
    (tw, th), baseline = cv2.getTextSize(message, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
    cv2.rectangle(out, (pad, pad), (pad + tw + 12, pad + th + baseline + 8), (0, 0, 0), -1)
    cv2.putText(out, message, (pad + 6, pad + th + 4),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 1, cv2.LINE_AA)
    return out
