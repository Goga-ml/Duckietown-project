"""Grab a single JPEG frame from a task server's MJPEG /video stream + print /status.

Lets an operator (or an agent) 'see' what the bot sees without a browser: it
connects to /video, extracts the first complete JPEG, writes it to a file, then
fetches /status so signs/state come alongside the image.

Usage:
    python scripts/grab_frame.py <host> [--port 5000] [--out frame.jpg] [--manual]
    --manual : first POST /set_mode {manual} so /keys driving is enabled and the
               autonomous controller is held off.
"""

import argparse
import sys

import requests

SOI = b"\xff\xd8"   # JPEG start-of-image
EOI = b"\xff\xd9"   # JPEG end-of-image


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--out", default="frame.jpg")
    ap.add_argument("--manual", action="store_true")
    ap.add_argument("--max-bytes", type=int, default=4_000_000)
    args = ap.parse_args()

    base = f"http://{args.host}:{args.port}"

    if args.manual:
        try:
            r = requests.post(f"{base}/set_mode", json={"mode": "manual"}, timeout=4)
            print(f"[grab] set_mode manual -> {r.status_code} {r.text.strip()[:80]}")
        except Exception as e:
            print(f"[grab] set_mode manual failed (ok if no such route): {e}")

    # Pull one JPEG out of the multipart MJPEG stream.
    try:
        with requests.get(f"{base}/video", stream=True, timeout=15) as resp:
            buf = b""
            for chunk in resp.iter_content(8192):
                buf += chunk
                s = buf.find(SOI)
                e = buf.find(EOI, s + 2) if s >= 0 else -1
                if s >= 0 and e >= 0:
                    jpg = buf[s:e + 2]
                    with open(args.out, "wb") as f:
                        f.write(jpg)
                    print(f"[grab] wrote {args.out} ({len(jpg)} bytes)")
                    break
                if len(buf) > args.max_bytes:
                    print("[grab] ERROR: no complete JPEG within max-bytes")
                    return 2
    except Exception as e:
        print(f"[grab] ERROR reading /video: {e}")
        return 2

    # Status alongside the frame (signs / state).
    try:
        st = requests.get(f"{base}/status", timeout=5).json()
        a = st.get("active_sign")
        print(f"[status] running={st.get('running')} manual={st.get('manual_mode')} "
              f"state={st.get('behavior_state')} stop_line={st.get('stop_line')}")
        print(f"[status] active_sign={a}")
        dets = st.get("detections")
        if dets:
            print(f"[status] detections={dets}")
    except Exception as e:
        print(f"[grab] (status unavailable: {e})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
