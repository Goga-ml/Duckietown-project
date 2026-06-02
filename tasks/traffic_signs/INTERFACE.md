# Traffic Signs — Perception → Behaviour Interface

This is the contract between **Person A (perception / sign recognition)** and
**Person B (behaviour / state machine)**. Person A produces sign observations;
Person B consumes them to drive the robot. Code against the shapes documented
here — they are intended to stay stable.

---

## TL;DR for Person B

Every frame, perception gives you **one "active sign" signal** — the single sign
the robot should react to right now — as a plain `dict` (or `None` if no relevant
sign is visible):

```python
{
  "tag_id":      9,                  # raw AprilTag id (debug/logging)
  "sign_type":   "stop",             # what the sign means (see vocabulary)
  "turns":       ["left", "right"],  # allowed turns at the intersection, or None
  "distance_m":  0.30,               # forward distance to the sign, metres
  "offset_norm": -0.12,              # lateral position in frame: -1 left .. +1 right
  "at_sign":     True,               # True once within AT_SIGN_DISTANCE_M
}
```

Your state machine mostly needs three things: **`sign_type`** (what to do),
**`at_sign`** (when to do it), and **`turns`** (which way to go at intersections).

---

## Where the signal comes from

Two equivalent ways to consume it, depending on where your state machine lives.

### Option A — in-process (recommended for the state machine)

This is how `servers/traffic_signs/{virtual,real}_server.py` already do it. Run
the agent on each frame and collapse the detections to the active signal:

```python
from tasks.traffic_signs.packages.agent import TrafficSignAgent
from tasks.traffic_signs.packages import detection_activity as signs

agent = TrafficSignAgent()           # loads config/traffic_signs_config.yaml

# ... per frame (RGB image) ...
detections = agent.detect(frame_rgb)         # list[SignDetection] | None
if detections is not None:                   # None == this frame was skipped
    active = signs.select_active_sign(detections)   # dict | None
    state_machine.update(active)
```

- `agent.detect()` returns `None` on intentionally skipped frames (frame-skip is
  `0` by default, so usually you get a list). When it returns `None`, **keep your
  previous signal** rather than treating it as "no sign".
- An empty list `[]` means "frame processed, no tags seen".

### Option B — over HTTP

Both servers expose the live signal at **`GET /status`**:

```json
{
  "running": true,
  "detector_ready": true,
  "family": "tag36h11",
  "active_sign": { ...the dict above... | null },
  "detections": [
    {"tag_id": 9, "sign_type": "T-intersection", "distance_m": 0.32,
     "turns": ["left","right"], "offset_norm": -0.38}
  ]
}
```

Poll `/status` if your state machine runs in a separate process. In-process
(Option A) is lower-latency and preferred.

---

## The active-sign dict (full spec)

| Key           | Type                  | Meaning / range |
|---------------|-----------------------|-----------------|
| `tag_id`      | `int`                 | Raw AprilTag id. For logging/debug; prefer `sign_type`. |
| `sign_type`   | `str`                 | One of the vocabulary strings below. Never `None` here. |
| `turns`       | `list[str]` or `None` | Allowed turns for **intersection** signs, e.g. `["left","straight"]`. `None` for non-intersection signs. |
| `distance_m`  | `float`               | Estimated forward distance to the sign in metres. Approximate — good for near/far gating; calibrate the camera for accuracy. |
| `offset_norm` | `float`               | Horizontal position of the sign in the frame: `-1.0` far left, `0` centre, `+1.0` far right. Useful to ignore cross-street signs. |
| `at_sign`     | `bool`                | `True` once `distance_m <= AT_SIGN_DISTANCE_M` (default 0.30 m). Your "act now" trigger. |

`select_active_sign()` already filters to **known signs roughly ahead of the
robot** and returns the **nearest** one. So you can trust the active sign is the
relevant one for the current approach.

---

## Sign vocabulary (`sign_type` values)

Defined in [`packages/sign_lookup.py`](packages/sign_lookup.py) → `SIGN_INFO`.

**Regulatory** (`category == "regulatory"`, `turns is None`):
- `"stop"` — stop sign: come to a full stop, then proceed.
- `"yield"` — yield sign: slow, give way to crossing traffic.

**Intersection / turn-option** (`category == "intersection"`, `turns` is a list):
- `"T-intersection"` — arriving at the stem of a T → `["left", "right"]`
- `"left-T-intersect"` — through-road continues straight + left → `["left", "straight"]`
- `"right-T-intersect"` — through-road continues straight + right → `["straight", "right"]`
- `"4-way-intersect"` — `["left", "straight", "right"]`

**Informational** (`category == "informational"`, `turns is None`):
- `"t-light-ahead"`, `"pedestrian"`, `"duck-crossing"`, `"parking"`,
  `"oneway-left"`, `"oneway-right"`

Turn codes are always the strings `"left"`, `"straight"`, `"right"`. To pick a
random allowed turn at an intersection (per the project spec):

```python
import random
if active and active["turns"]:
    chosen = random.choice(active["turns"])   # "left" | "straight" | "right"
```

> The mapping from **tag id → sign_type** is town-specific. The defaults in
> `sign_lookup.py` are examples; the real numbers for your map go in
> `config/traffic_signs_config.yaml` under a `signs:` block. This does **not**
> change the interface — `sign_type`/`turns` strings stay the same.

---

## Raw detections (if you need more than the active sign)

`agent.detect()` returns a list of `SignDetection` dataclasses
([`packages/agent.py`](packages/agent.py)). Useful if your state machine wants to
reason about multiple signs at once (e.g. several intersection signs in view):

| Field         | Type                          | Notes |
|---------------|-------------------------------|-------|
| `tag_id`      | `int`                         | |
| `sign_type`   | `str` or `None`               | `None` if the tag id isn't a known sign |
| `turns`       | `list[str]` or `None`         | |
| `corners`     | `np.ndarray` (4,2)            | tag corners in pixels (TL, TR, BR, BL) |
| `center`      | `(float, float)`              | tag centre in pixels |
| `bbox`        | `(x1, y1, x2, y2)`            | axis-aligned, pixels |
| `pixel_size`  | `float`                       | mean edge length in pixels (apparent size) |
| `distance_m`  | `float`                       | forward distance, metres |
| `offset_norm` | `float`                       | -1 .. +1 (see above) |

`select_active_sign()` is just a policy over this list; if you want a different
selection policy (e.g. "nearest stop sign only"), write your own over the same
list rather than changing the dict schema.

---

## Tuning knobs you may care about

In [`packages/detection_activity.py`](packages/detection_activity.py) (Person A
owns these, but they affect your timing):

- `AT_SIGN_DISTANCE_M` (default `0.30`) — distance at which `at_sign` flips True.
  This is effectively *where your state machine acts*. Tune together.
- `filter_by_distance` / `filter_by_size` — reject far/tiny spurious tags.
- `NUMBER_FRAMES_SKIPPED()` — detector frame-skip (default 0 = every frame).

---

## Suggested state-machine skeleton

A minimal example of how Person B might consume the signal. This is a sketch, not
part of the perception layer — put it in your own module.

```python
import random

class SignStateMachine:
    """Consumes the active-sign dict and emits a driving intent."""

    def __init__(self):
        self.state = "DRIVING"     # DRIVING | STOPPING | WAITING | TURNING
        self._chosen_turn = None

    def update(self, active):
        # active: dict | None  (keep last if perception returned None upstream)
        if self.state == "DRIVING":
            if active and active["at_sign"]:
                if active["sign_type"] == "stop":
                    self.state = "STOPPING"
                elif active["turns"]:                      # intersection sign
                    self._chosen_turn = random.choice(active["turns"])
                    self.state = "TURNING"
                elif active["sign_type"] == "yield":
                    self.state = "WAITING"

        elif self.state == "STOPPING":
            # command a smooth stop; once stopped and clear -> resume
            if self._stopped_and_clear():
                self.state = "DRIVING"

        elif self.state == "TURNING":
            # execute self._chosen_turn through the intersection
            if self._turn_complete():
                self._chosen_turn = None
                self.state = "DRIVING"

        elif self.state == "WAITING":
            if self._crossing_clear():
                self.state = "DRIVING"

    # _stopped_and_clear / _turn_complete / _crossing_clear: Person B implements,
    # using wheels/encoders and (optionally) the object_detection agent for the
    # "stop for obstacles / crossing traffic" parts.
```

Wire it into a copy of `servers/traffic_signs/real_server.py`: in the
`visualize()` loop you already have `active` available — call
`state_machine.update(active)` and translate its state into wheel commands
(replacing the plain lane-follow passthrough).

---

## Quick self-check before integrating

```bash
# synthetic tag -> detection -> active signal (no robot needed)
python -m tasks.traffic_signs.packages.test_detector

# live, with a printed tag in front of a webcam
python -m tasks.traffic_signs.packages.test_detector --webcam
```
