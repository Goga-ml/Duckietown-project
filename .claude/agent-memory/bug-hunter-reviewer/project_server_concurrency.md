---
name: project-server-concurrency
description: How the Duckietown Flask servers structure their wheel control loop and the recurring concurrency hazards around it
metadata:
  type: project
---

The per-task Flask servers (servers/<task>/{real,virtual}_server.py) all share a
control architecture worth scrutinising for races:

- The wheel control loop lives **inside `visualize(frame)`**, which is invoked
  per-frame by `make_frame_generator` (servers/common.py) on the Flask MJPEG
  streaming thread. So "who drives the wheels" is decided per video frame.
- Background daemon threads (`detection_loop`, `obstacle_loop`,
  `manual_control_loop`) also run; shared detection state is guarded by
  `_detection_lock` / `_surroundings_lock` / `_keys_lock`.
- BUT the control-flow flags `running`, `manual_mode`, and (in traffic_signs)
  `_last_tick_t` / `_prev_state` are plain module globals with **no lock**.

Recurring hazards (verify each time a server changes):

1. **No single-client guard on `/video`.** Two browsers hitting `/video` run
   `generate()` (hence `visualize`) on two threads simultaneously. They both
   call `wheels.set_wheels_speed(...)` and mutate the unlocked control globals.
   In traffic_signs this corrupts `dt` (shared `_last_tick_t`) and thrashes
   `_prev_state`, defeating slew limiting and the lane-reset edge. Pre-existing
   pattern in every server, but only harmful once control state lives in
   visualize (traffic_signs). ALSO note: the shared `MotionController` instance
   (`motion._left/_right`) is itself unlocked mutable state both threads step()
   — an even more direct race than the dt one. Calibration on the "frozen slew"
   framing: clamped-to-0.01 dt is the WORST-case tight interleave; the typical
   effect is dt roughly halved (~2x slower ramp), not literally frozen. Confirmed
   genuine + high severity on re-review 2026-06-02 (real_server.py:156-176,344).

2. **/stop and /set_mode(auto) TOCTOU.** `visualize` reads `running`/`manual_mode`
   then later writes wheels; a route handler can flip the flag + zero the wheels
   in between, and visualize's later write re-drives for one frame. In
   traffic_signs the `not running` branch writes a hard 0 every tick so it
   self-heals next frame (one stray frame of motion). object_detection eases via
   `_speed_scale` so it also self-heals.

3. **manual->auto stale write.** `manual_control_loop` can read `manual_mode`
   True then execute `set_wheels_speed` after `/set_mode` switched to auto,
   overriding the route's zero for one tick (~50ms). Symmetric across servers.

Motion smoothness ("no jerky stops") is a grading criterion enforced by
MotionController slew-limiting in tasks/traffic_signs/packages/state_machine.py.
Anything that bypasses MotionController (e.g. the hard-zero `not running` branch,
or manual loop) is intentionally exempt but worth checking against that rubric.
