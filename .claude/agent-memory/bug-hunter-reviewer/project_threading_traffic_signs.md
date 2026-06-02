---
name: project-threading-traffic-signs
description: traffic_signs real_server daemon loops, lock-guarded state, and uncaught-exception thread-death risk
metadata:
  type: project
---

servers/traffic_signs/real_server.py runs daemon threads: detection_loop (signs),
obstacle_loop (SurroundingsSensor), manual_control_loop. visualize() runs per
frame on the Flask MJPEG thread. Shared state guarded by _detection_lock and
_surroundings_lock.

**Risk pattern:** obstacle_loop only wraps `_obstacle_queue.get()` in try/except.
`surround_sensor.update()` (cv2.resize + agent.detect() whose _postprocess is
OUTSIDE detect()'s own try/except + should_stop) can raise; an uncaught raise
PERMANENTLY kills the daemon loop, silently disabling obstacle-stop AND
right-of-way. The sensor docstring promises "degrades gracefully" — an uncaught
exception violates that. Same shape would apply to detection_loop.

**Reset gap:** /stop->/start calls _reset_behavior() which resets sign_sm, motion,
lane PD, _prev_state, _last_tick_t — but NOT SurroundingsSensor hysteresis
(_robot_present/_set_streak/_clear_streak/_last) nor stop_activity's module-global
should_stop state (_state/_clear_streak). Stale obstacle/right-of-way can briefly
re-assert after a restart until live detection self-corrects.

**How to apply:** When reviewing changes to these loops or the sensor, check (1)
exceptions in update() are contained, (2) any state that should be cleared on
/start is actually cleared.
