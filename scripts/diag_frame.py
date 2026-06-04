"""One-off diagnostic: run the lane + stop-line detectors on a captured frame
and dump the masks + HSV stats, so we can see exactly what the bot 'sees'.

Usage:  python scripts/diag_frame.py [frame.jpg]
Writes diag_*.png next to the input and prints pixel counts / HSV ranges.
"""
import os
import sys

import cv2
import numpy as np

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tasks.visual_lane_servoing.packages import visual_servoing_activity as vsa
from tasks.traffic_signs.packages.stop_line import StopLineDetector


def hsv_stats(hsv, mask=None):
    if mask is not None:
        sel = hsv[mask > 0]
    else:
        sel = hsv.reshape(-1, 3)
    if len(sel) == 0:
        return "  (empty)"
    h, s, v = sel[:, 0], sel[:, 1], sel[:, 2]
    return (f"  H[{h.min():3d}..{h.max():3d}] med {int(np.median(h)):3d} | "
            f"S[{s.min():3d}..{s.max():3d}] med {int(np.median(s)):3d} | "
            f"V[{v.min():3d}..{v.max():3d}] med {int(np.median(v)):3d}  (n={len(sel)})")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'frame.jpg')
    bgr = cv2.imread(path)
    if bgr is None:
        print(f"could not read {path}")
        return 2
    h, w = bgr.shape[:2]
    print(f"frame {path}  {w}x{h}")

    # ---- Lane detection (detect_lane_markings expects BGR; imread IS bgr) ----
    print("\n== current lane HSV bounds ==")
    print("  yellow:", vsa._yellow_lower.tolist(), "->", vsa._yellow_upper.tolist())
    print("  white :", vsa._white_lower.tolist(),  "->", vsa._white_upper.tolist())

    y_mask, w_mask = vsa.detect_lane_markings(bgr)
    yc, wc = int(y_mask.sum()), int(w_mask.sum())
    print(f"\n== lane masks with CURRENT bounds ==")
    print(f"  yellow pixels: {yc}")
    print(f"  white  pixels: {wc}")

    # What hue is actually present in the road ROI (bottom 60%)?
    roi = bgr[int(h * 0.4):, :]
    hsv = cv2.cvtColor(cv2.GaussianBlur(roi, (5, 5), 2), cv2.COLOR_BGR2HSV)
    print("\n== HSV of the whole road ROI (bottom 60%) ==")
    print(hsv_stats(hsv))

    # Try a CORRECT yellow band (hue ~15-40) to see if a yellow line exists.
    test_yellow = cv2.inRange(hsv, (15, 60, 60), (40, 255, 255))
    print(f"\n== test yellow mask hue[15..40] s>60 v>60: {int(np.count_nonzero(test_yellow))} px ==")
    if np.count_nonzero(test_yellow):
        print("  HSV of those px:", hsv_stats(hsv, test_yellow))

    # Save mask visualizations
    out_y = np.zeros((h, w, 3), np.uint8); out_y[y_mask > 0] = (0, 255, 255)
    out_w = np.zeros((h, w, 3), np.uint8); out_w[w_mask > 0] = (255, 255, 255)
    cv2.imwrite(os.path.join(ROOT, 'diag_yellow_mask.png'), out_y)
    cv2.imwrite(os.path.join(ROOT, 'diag_white_mask.png'), out_w)

    ty_full = np.zeros((h, w, 3), np.uint8)
    ty_full[int(h * 0.4):, :][test_yellow > 0] = (0, 165, 255)
    cv2.imwrite(os.path.join(ROOT, 'diag_test_yellow.png'), ty_full)

    # ---- Stop-line detector (expects RGB) ----
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    det = StopLineDetector()
    hit = det.detect(rgb)
    print(f"\n== stop-line detector ==")
    print(f"  ROI top={det.roi_top} x[{det.roi_x0}..{det.roi_x1}] "
          f"h_lo={det.h_lo} h_hi={det.h_hi} s_min={det.s_min} v_min={det.v_min} "
          f"min_area={det.min_area_ratio}")
    print(f"  last_ratio={det.last_ratio:.4f}  -> {'RED LINE (would STOP)' if hit else 'clear'}")

    # red mask over the stop-line ROI for viewing
    y0 = int(h * det.roi_top); x0 = int(w * det.roi_x0); x1 = int(w * det.roi_x1)
    sroi = rgb[y0:h, x0:x1]
    shsv = cv2.cvtColor(sroi, cv2.COLOR_RGB2HSV)
    m1 = cv2.inRange(shsv, (0, det.s_min, det.v_min), (det.h_lo, 255, 255))
    m2 = cv2.inRange(shsv, (det.h_hi, det.s_min, det.v_min), (179, 255, 255))
    redmask = cv2.bitwise_or(m1, m2)
    red_vis = bgr.copy()
    sub = red_vis[y0:h, x0:x1]
    sub[redmask > 0] = (0, 0, 255)
    cv2.rectangle(red_vis, (x0, y0), (x1, h - 1), (255, 255, 0), 2)
    cv2.imwrite(os.path.join(ROOT, 'diag_stopline.png'), red_vis)
    print(f"  red px in ROI: {int(np.count_nonzero(redmask))} / {redmask.size}")

    print("\nwrote diag_yellow_mask.png diag_white_mask.png diag_test_yellow.png diag_stopline.png")
    return 0


if __name__ == '__main__':
    sys.exit(main())
