---
name: project-traffic-signs-fsm
description: Bug-prone areas in tasks/traffic_signs/packages/state_machine.py (Person B behaviour FSM + MotionController)
metadata:
  type: project
---

The traffic_signs behaviour layer (Person B) is `tasks/traffic_signs/packages/state_machine.py`:
`TrafficSignStateMachine` (pure FSM) + `MotionController` (slew-limited wheel speeds) +
`Surroundings`. Consumed by `servers/traffic_signs/real_server.py` `visualize()`.

FIXED (verified 2026-06-02, all 49 self-check + manual trace analysis):

- **Cooldown -> tag-id latch (FIXED).** Replaced time-only `_cooldown_until` with a tag-keyed
  latch: `_mark_handled()` sets `_handled_tag=_target_tag` + `_cooldown_until=_now+cooldown_s`;
  `_suppressed(active)` only blocks `tag_id == _handled_tag` (a DIFFERENT tag is never blocked);
  `_update_sign_latch` (called only in `_h_driving`) clears `_handled_tag` after the min-hold
  once the sign is gone / a different tag / receded past approach range. Cannot permanently
  suppress all signs (suppression is per-tag) and cannot stick (clears the tick the same tag
  stops being the in-zone active sign). Both failure modes resolved.
- **WAITING timeout (FIXED).** `_h_waiting`: `waited_out = _state_time >= right_of_way_max_wait_s`
  (default 5s). `robot_on_right` no longer holds once waited_out; `obstacle_ahead` still holds
  unconditionally. `_state_time` is reset on every `_goto`, so it is the correct in-state clock.
- **Snap-to-zero slew bound (FIXED).** `MotionController.step` now uses `snap=min(snap_zero,
  max_step)` and `_advance` makes snap (target==0 & |current|<=snap -> 0) MUTUALLY EXCLUSIVE
  with slew (`_approach`). Per-wheel per-tick change is provably <= max_step at any dt
  (max_step uses `max_accel*max(1e-3,dt)`). Arc targets with a 0.0 wheel (e.g. turn_left_left=0)
  snap that wheel to its correct 0 target — behaviourally correct, not a bug.
- `_last_dist` still written in three places but never read (dead state — harmless).
- `_classify` ordering relies on the INTERFACE contract that stop/yield have `turns is None`
  and intersection signs have a non-empty `turns` list. Correct given the contract; would
  misclassify if perception ever set `turns` on a regulatory sign.
