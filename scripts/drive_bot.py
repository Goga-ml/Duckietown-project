"""Safe remote manual-drive helper for the Duckiebot task servers.

The task servers (traffic_signs, ...) expose a manual-drive backend that works
over HTTP regardless of which web UI is loaded:

  POST /set_mode {"mode": "manual"}   -> hand the wheels to manual control
  POST /keys     {"up":bool, ...}     -> set the held arrow keys
  (a 0.5 s watchdog on the bot zeroes the wheels if /keys stops arriving)

This script drives ONE bounded motion and then *guarantees* a stop, even on
error / Ctrl-C. It re-sends the key every CADENCE seconds so the watchdog never
trips mid-move, and it hard-caps the duration so a bug can't run the bot away.

Usage:
    python scripts/drive_bot.py <host> <action> [seconds]
    python scripts/drive_bot.py 192.168.1.42 forward 1.0
    python scripts/drive_bot.py kvati.local stop          # emergency stop

actions: forward back left right fl(fwd-left) fr(fwd-right) stop
Options: --port (default 5000)  --max (hard duration cap, default 3.0s)
         --cadence (re-post interval, default 0.15s)  --leave-manual

Speeds are fixed by the server's manual mapping (forward 0.5, turn 0.3); this
script only chooses which keys are held and for how long. Start with short
nudges to confirm wheel polarity before longer moves.
"""

import argparse
import sys
import time

import requests

# action -> which arrow keys are held (matches the server's manual mapping)
_ACTIONS = {
    "forward": {"up": True},
    "back":    {"down": True},
    "left":    {"left": True},          # spin in place (left wheel back, right fwd)
    "right":   {"right": True},         # spin in place
    "fl":      {"up": True, "left": True},
    "fr":      {"up": True, "right": True},
    "stop":    {},                      # all keys released
}
_KEYS = ("up", "down", "left", "right")


def _url(host, port, path):
    return f"http://{host}:{port}{path}"


def _keys_payload(held):
    return {k: bool(held.get(k, False)) for k in _KEYS}


def _post(host, port, path, body, timeout=2.0):
    return requests.post(_url(host, port, path), json=body, timeout=timeout)


def _all_stop(host, port, leave_manual):
    """Best-effort guaranteed stop: release keys, then drop out of manual mode
    (which zeroes the wheels server-side) unless asked to stay in manual."""
    try:
        _post(host, port, "/keys", _keys_payload({}))
    except Exception as e:
        print(f"[drive] WARN: stop /keys failed: {e}")
    if not leave_manual:
        try:
            _post(host, port, "/set_mode", {"mode": "auto"})
        except Exception as e:
            print(f"[drive] WARN: set_mode auto failed: {e}")


def main():
    ap = argparse.ArgumentParser(description="Safe remote manual drive for a Duckiebot task server")
    ap.add_argument("host", help="bot hostname or IP (e.g. 192.168.1.42 or kvati.local)")
    ap.add_argument("action", choices=sorted(_ACTIONS), help="motion to perform")
    ap.add_argument("seconds", nargs="?", type=float, default=1.0, help="how long to hold it (default 1.0)")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--max", type=float, default=3.0, help="hard cap on seconds (safety)")
    ap.add_argument("--cadence", type=float, default=0.15, help="re-post interval (< 0.5s watchdog)")
    ap.add_argument("--leave-manual", action="store_true", help="stay in manual mode after stopping")
    args = ap.parse_args()

    # Preflight: confirm the server is reachable. Different task servers expose
    # different status endpoints (/status on mine, /speeds on the stock
    # introduction server), so accept any that answers.
    reached = False
    last = None
    for ep in ("/status", "/speeds", "/"):
        try:
            r = requests.get(_url(args.host, args.port, ep), timeout=3)
            if r.status_code < 500:
                info = ""
                try:
                    info = str(r.json())
                except Exception:
                    info = f"HTTP {r.status_code}"
                print(f"[drive] reachable via {ep}: {info}")
                reached = True
                break
        except Exception as e:
            last = e
    if not reached:
        print(f"[drive] ERROR: cannot reach {args.host}:{args.port} (last: {last})")
        return 2

    held = _ACTIONS[args.action]
    if args.action == "stop" or not held:
        print("[drive] STOP")
        _all_stop(args.host, args.port, args.leave_manual)
        return 0

    dur = max(0.0, min(args.seconds, args.max))
    if dur != args.seconds:
        print(f"[drive] duration capped to {dur:.2f}s (max {args.max:.2f}s)")

    try:
        _post(args.host, args.port, "/set_mode", {"mode": "manual"})
        print(f"[drive] {args.action} for {dur:.2f}s (keys={[k for k in _KEYS if held.get(k)]})")
        t_end = time.monotonic() + dur
        while time.monotonic() < t_end:
            _post(args.host, args.port, "/keys", _keys_payload(held))
            time.sleep(args.cadence)
    except KeyboardInterrupt:
        print("\n[drive] interrupted -> stopping")
    except Exception as e:
        print(f"[drive] ERROR mid-move -> stopping: {e}")
    finally:
        _all_stop(args.host, args.port, args.leave_manual)
        print("[drive] stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
