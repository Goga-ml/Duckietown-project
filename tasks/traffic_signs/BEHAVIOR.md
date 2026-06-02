# Traffic Signs — Behaviour Layer (Person B)

This is the **behaviour / control half** of the traffic-signs project: the part
that decides what the Duckiebot *does* when perception reports a sign. The
perception half (Person A — AprilTag recognition) is unchanged; we only consume
its output through the documented contract in
[INTERFACE.md](INTERFACE.md).

> **Platform note.** Despite the generic "ROS topics / message types" framing in
> the brief, this repo is **not ROS** — it's the Flask + Godot architecture
> described in `CLAUDE.md`. Perception is consumed **in-process** (the
> recommended Option A in INTERFACE.md), not over a ROS topic.

---

## 1. Where the code lives (and why)

| File | Role | Ships to bot via |
|------|------|------------------|
| `tasks/traffic_signs/packages/state_machine.py` | All decision logic + smooth motion | `--run` (packaged automatically) |
| `tasks/traffic_signs/packages/sensors.py` | Obstacle / other-robot sensing | `--run` |
| `tasks/traffic_signs/packages/test_state_machine.py` | Hardware-free unit tests | `--run` |
| `config/traffic_signs_config.yaml` → `behavior:` | All tunables | `--run` |
| `servers/traffic_signs/real_server.py` | Thin glue: calls the state machine | **`git pull`** (servers/ is not packaged) |
| `servers/traffic_signs/visualization.py` | State overlay on the video feed | `git pull` |

The split is deliberate: **stable plumbing stays in the server, tunable
behaviour stays in `packages/`** (the project convention), and crucially the
behaviour logic ships on every `--run` while the server is updated by pulling on
the bot.

The design follows three pieces that keep *state logic separate from motor
commands* (a grading requirement):

1. **`TrafficSignStateMachine`** — pure decision logic. `update(active,
   surroundings, dt)` advances the state machine and returns a high-level
   `DriveCommand` ("follow the lane at 40 %", "halt", "arc left"). It knows
   nothing about wheels, the camera, OpenCV, or the lane agent — so it is fully
   unit-testable with plain dicts (no robot).
2. **`MotionController`** — turns a `DriveCommand` (+ the lane agent's raw
   command) into concrete wheel speeds and **slew-rate-limits** them so motion
   is always smooth.
3. **`Surroundings`** + **`SurroundingsSensor`** — the obstacle / other-robot
   signal, produced from the object-detection model.

---

## 2. The contract we consume

Every frame, perception gives us one **active-sign dict** (or `None`):

```python
{ "tag_id": 9, "sign_type": "stop", "turns": ["left","right"] | None,
  "distance_m": 0.30, "offset_norm": -0.12, "at_sign": True }
```

The state machine only needs three of these: **`sign_type`** (what to do),
**`at_sign`** / **`distance_m`** (when to do it), and **`turns`** (which way at
an intersection). `None` means "no relevant sign right now" and is handled
explicitly (we don't treat a 1-frame dropout as "the sign vanished").

---

## 3. The state machine

Six explicit, clearly-named states:

```
        ┌──────────────────────────── obstacle ahead ───────────────────────────┐
        │                                                                        ▼
   ┌──────────┐  sign within     ┌──────────────────┐  at sign      ┌──────────────┐
   │ DRIVING  │ ───approach m──▶ │ APPROACHING_SIGN │ ───(stop /    │ OBSTACLE_STOP│
   │ (cruise) │                  │ (decelerate)     │   intersect)─▶ └──────┬───────┘
   └────▲─────┘                  └────────┬─────────┘                       │ clear
        │                                 │ at sign (yield)                 ▼
        │ proceed / turn done             │                          back to DRIVING
        │                                 ▼
        │                        ┌──────────────┐  pause done   ┌───────────────────────────┐
        │                        │   STOPPED    │ ────────────▶ │ WAITING_FOR_RIGHT_OF_WAY  │
        │                        │ (full stop,  │               │ ("right has precedence")  │
        │                        │  hold pause) │               └───────────┬───────────────┘
        │                        └──────────────┘                clear & no turn │  clear & turn
        │                                                                        │
        │                                              ┌────────────┐           │
        └──────────────────────────────────────────── │  TURNING   │ ◀─────────┘
                                  turn complete         │ (open-loop │
                                                        │  arc)      │
                                                        └────────────┘
```

### Transition table — *why each transition fires*

| From | To | Fires when | Why |
|------|----|-----------|-----|
| DRIVING | OBSTACLE_STOP | `surroundings.obstacle_ahead` | Safety first — an obstacle dead ahead overrides everything. |
| DRIVING | APPROACHING_SIGN | a stop/yield/intersection sign with `distance_m ≤ approach_distance_m` and not in cooldown | Start reacting *before* the sign so we can decelerate smoothly. The random turn is chosen **here** (at trigger) for an intersection sign. |
| APPROACHING_SIGN | OBSTACLE_STOP | `obstacle_ahead` | Safety override still applies while approaching. |
| APPROACHING_SIGN | STOPPED | `at_sign` true (stop or intersection sign) | Stop/intersection signs require a full stop at the line. |
| APPROACHING_SIGN | WAITING_FOR_RIGHT_OF_WAY | `at_sign` true (yield sign) | A yield doesn't fully stop — it slows and checks traffic. |
| STOPPED | WAITING_FOR_RIGHT_OF_WAY | `stop_pause_s` elapsed | After the mandatory pause, check who has priority before moving. |
| WAITING_FOR_RIGHT_OF_WAY | (stay) | `robot_on_right` (until `right_of_way_max_wait_s`) or `obstacle_ahead` | "Right has precedence" — give way to a vehicle on the right. A bounded timeout then proceeds so a static object can't deadlock us; an obstacle *directly ahead* holds unconditionally (never drive into it). |
| WAITING_FOR_RIGHT_OF_WAY | TURNING | clear **and** a turn was chosen | Intersection sign → execute the random valid turn. |
| WAITING_FOR_RIGHT_OF_WAY | DRIVING | clear **and** no turn | Stop/yield with no turn → just proceed. |
| TURNING | DRIVING | turn duration elapsed | Open-loop arc finished; hand back to lane following. |
| OBSTACLE_STOP | DRIVING | `not obstacle_ahead` | Obstacle gone → resume. |

Two cross-cutting mechanisms keep it robust:

- **Handled-sign latch (keyed on `tag_id`)** — after finishing a sign we latch
  *that sign's AprilTag id* so we don't stop a second time at it while it
  lingers in view as we pull away. The latch holds for at least `cooldown_s`
  (riding out 1-frame perception dropouts) and clears once the sign actually
  leaves the reaction zone. Crucially it's keyed on `tag_id`, so a **different**
  sign right behind it (e.g. a stop sign then a turn-option sign at the same
  junction) is never suppressed — it's handled immediately.
- **Lost-sign grace (`lost_grace_s`)** — AprilTags slip above the camera when
  you're right on top of them. If the sign vanishes during APPROACHING_SIGN we
  keep decelerating and treat it as "arrived" after a short grace, instead of
  speeding back up. This also rides out single-frame perception dropouts.

---

## 4. Smooth motion (the "no jerky stops" guarantee)

All wheel commands pass through `MotionController.step()`, which **slew-rate
limits** every output: the command can change by at most `max_accel * dt` per
tick. No matter how abruptly the state machine changes its mind (cruise → halt →
arc), the wheels can only ramp. This is a *mathematical* bound on jerk, and the
unit test asserts it on every tick of every scenario.

Two extra touches:
- During APPROACHING_SIGN the *target* speed is also ramped down in proportion
  to remaining distance, so the bot eases to rest right at the sign rather than
  braking hard at the last moment.
- A near-zero target snaps to a hard zero (`snap_zero`) so the bot actually
  stops instead of dribbling below the motor deadzone.

When we hand control back to the lane follower (after a stop/turn/obstacle) we
**reset the lane agent's PD state** (`reset_lane_follower`) so its stale
filtered error doesn't cause a one-off steering lurch on resume.

---

## 5. Obstacle stopping & two-robot right-of-way

Traffic-sign perception only sees AprilTags, so these two "is the way clear?"
questions are answered by reusing the project's **object-detection YOLO model**
(`tasks/object_detection`), in `sensors.py`:

- **Obstacle ahead** — delegated to object_detection's own `should_stop()`, so
  we inherit its tuned enter/exit hysteresis verbatim. A duckie/truck in the
  central zone → stop; clears after several empty frames → resume.
- **Robot on the right** — the detector has **no "robot" class** (only
  `duckie`, `truck`, `sign`), so we treat any detected *vehicle* sitting in the
  right region of the frame as the other robot. This is **detection, not
  communication** — the two robots never talk to each other, satisfying the
  requirement. A small hysteresis prevents the WAITING state from flapping.

Both run in the detector's square `img_size` pixel space (we feed it a
pre-resized square frame, exactly like the object-detection server) so the
reused `should_stop` sees the coordinates it expects.

> **Limitation to state out loud:** because there is no robot class, the
> right-of-way check cannot distinguish another Duckiebot from, say, a rubber
> duck on the right. If you want a stronger signal, mount an AprilTag on the
> other robot and read it from the raw detection list (the interface exposes it)
> — the architecture already isolates this behind `Surroundings`, so only
> `sensors.py` would change.

---

## 6. Running & testing

Because the `traffic_signs` Godot scene reuses the lane-following scene and has
**no AprilTags and no second robot**, `--sim` can only validate lane-following +
the plumbing — *not* sign reactions, turns, obstacle stops, or right-of-way.
So the behaviour is validated two ways:

**(a) Hardware-free unit tests (the main verification path):**
```bash
python -m tasks.traffic_signs.packages.test_state_machine
```
Drives the state machine with synthetic perception dicts and `Surroundings`
snapshots through all five stages, and checks the smoothness bound numerically
on every tick. Runs with only the standard library (no robot, camera, or model).

**(b) On the real robot:**
```bash
# behaviour code + config ship automatically:
python launch.py --run --bot <hostname> --task traffic_signs
# server glue lands via git on the bot:  git pull   (on the Duckiebot)
```
Open the web UI, press **Start**. The video overlay shows the live state
(`STATE: APPROACHING_SIGN | approaching stop`), and `GET /status` returns
`behavior_state`, `chosen_turn`, `obstacle_ahead`, `robot_on_right`.

**For obstacle-stop & right-of-way you also need the object-detection model on
the bot:** `best.onnx` is git-ignored and is *not* shipped by `traffic_signs
--run`, so place it at `tasks/object_detection/models/best.onnx`. If it's
missing the sensor reports "all clear" and only those two behaviours go inert —
signs, stops, and turns still work.

---

## 7. What still needs verification / calibration on the real bot

The logic is verified; these **physical** numbers must be tuned on your robot &
track (all live in `config/traffic_signs_config.yaml → behavior:`):

- **Turn arcs** (`turn_*_left/right/_s`) — open-loop timed turns. Tune each
  duration to ≈90° and the left/right split to your turn radius. *Highest
  priority to calibrate* for "clean turns".
- **`approach_distance_m` / `at_sign_distance_m`** — keep `at_sign_distance_m`
  equal to perception's `AT_SIGN_DISTANCE_M` (0.30 m). Widen `approach_distance_m`
  for a gentler stop if the bot drives faster.
- **`max_accel`** — smaller = smoother but laggier; larger = snappier.
- **`right_of_way_*`** — the right-region box and hysteresis; tune to where the
  other robot actually appears in frame at your intersections.
- **`right_of_way_max_wait_s`** — how long to give way before proceeding anyway
  (so a static object on the right doesn't deadlock the intersection).
- **Camera intrinsics & `tag.size_m`** (perception config) — distance accuracy
  drives *when* the stop happens; calibrate for reliable `at_sign` timing.
