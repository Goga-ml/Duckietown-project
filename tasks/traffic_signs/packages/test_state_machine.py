"""Standalone, hardware-free tests for the traffic-sign behaviour layer.

Run it like the perception self-check (no robot, no camera, no model):

    python -m tasks.traffic_signs.packages.test_state_machine

Why this exists: the traffic_signs Godot scene has no AprilTags and no second
robot, so ``--sim`` cannot exercise sign reactions, turns, obstacle stops, or
the two-robot right-of-way rule. This harness drives the state machine with
synthetic perception ``active`` dicts and synthetic ``Surroundings`` snapshots
so every stage of the build can be validated off-robot, including the
"motion is always smooth" grading criterion (checked numerically on every tick).

Exit code is non-zero if any check fails.
"""

import sys
from dataclasses import replace

from tasks.traffic_signs.packages.state_machine import (
    TrafficSignStateMachine, MotionController, BehaviorConfig, Surroundings,
    reset_lane_follower,
    DRIVING, APPROACHING_SIGN, STOPPED, WAITING_FOR_RIGHT_OF_WAY,
    TURNING, OBSTACLE_STOP, YIELDING,
)
import random

# ---------------------------------------------------------------------------
# Tiny test framework
# ---------------------------------------------------------------------------
_failures = []


def check(cond, msg):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {msg}")
    if not cond:
        _failures.append(msg)


def mk_active(sign_type, distance_m, turns=None, at_sign_distance=0.30, tag_id=0,
              pixel_size=0.0):
    """Build a perception active-sign dict matching INTERFACE.md.

    ``pixel_size`` defaults to 0.0 so the behaviour's apparent-size gates stay
    inert and tests that only vary ``distance_m`` exercise the metric path
    exactly as before; set it to drive the size-based triggers."""
    return {
        "tag_id": tag_id,
        "sign_type": sign_type,
        "turns": turns,
        "distance_m": round(distance_m, 3),
        "offset_norm": 0.0,
        "pixel_size": round(pixel_size, 1),
        "at_sign": distance_m <= at_sign_distance,
    }


class Sim:
    """Drives sm+motion at a fixed tick rate and verifies smoothness live."""

    def __init__(self, cfg=None, lane=(0.4, 0.4), dt=0.05, seed=1):
        self.cfg = cfg or BehaviorConfig()
        self.sm = TrafficSignStateMachine(self.cfg, rng=random.Random(seed))
        self.motion = MotionController(self.cfg)
        self.lane = lane
        self.dt = dt
        self.max_step = self.cfg.max_accel * dt
        self.prev = (0.0, 0.0)
        self.worst_jump = 0.0
        self.smooth_ok = True
        self.trace = []

    def tick(self, active=None, surr=None):
        cmd = self.sm.update(active, surr, self.dt)
        left, right = self.motion.step(cmd, self.lane, self.dt)
        dl, dr = abs(left - self.prev[0]), abs(right - self.prev[1])
        self.worst_jump = max(self.worst_jump, dl, dr)
        if dl > self.max_step + 1e-9 or dr > self.max_step + 1e-9:
            self.smooth_ok = False
        self.prev = (left, right)
        rec = dict(state=self.sm.state, kind=cmd.kind, scale=cmd.speed_scale,
                   left=left, right=right, turn=self.sm.chosen_turn)
        self.trace.append(rec)
        return rec

    def run(self, n, active=None, surr=None):
        rec = None
        for _ in range(n):
            rec = self.tick(active, surr)
        return rec

    def approach(self, sign_type, turns=None, start=0.58, stop=0.24, step=0.03,
                 surr=None, line_frames=3):
        """Feed a shrinking-distance approach, then arrive at the red stop line.

        The machine latches the action when it first sees the sign, keeps
        lane-following, and only acts once it reaches the red stop line — so the
        approach ends with a few frames carrying ``stop_line_ahead=True`` (the
        sign is typically out of view by then, hence ``active=None``)."""
        base = surr or CLEAR
        d = start
        last = None
        while d >= stop - 1e-9:
            last = self.tick(mk_active(sign_type, d, turns), base)
            d -= step
        line_surr = replace(base, stop_line_ahead=True)
        for _ in range(line_frames):
            last = self.tick(None, line_surr)
        return last

    def states_seen(self):
        seen, order = set(), []
        for r in self.trace:
            if r['state'] not in seen:
                seen.add(r['state'])
                order.append(r['state'])
        return order

    def reached(self, state):
        return any(r['state'] == state for r in self.trace)


CLEAR = Surroundings.clear()


# ---------------------------------------------------------------------------
# Stage 1 — pipeline / no sign: just cruise
# ---------------------------------------------------------------------------
def test_stage1_cruise():
    print("\nStage 1: cruise when there is no sign")
    s = Sim()
    s.run(20, active=None, surr=CLEAR)
    check(s.sm.state == DRIVING, "stays in DRIVING with no sign")
    check(s.trace[-1]['kind'] == 'lane_follow', "emits lane_follow command")
    check(abs(s.trace[-1]['left'] - 0.4) < 1e-6 and abs(s.trace[-1]['right'] - 0.4) < 1e-6,
          "ramps up to the lane command (0.4, 0.4)")
    check(s.smooth_ok, f"motion stayed smooth (worst jump {s.worst_jump:.3f} <= {s.max_step:.3f})")


# ---------------------------------------------------------------------------
# Stage 2 — stop sign: smooth decel -> full stop -> pause -> resume
# ---------------------------------------------------------------------------
def test_stage2_stop_sign():
    print("\nStage 2: stop sign (smooth stop, pause, resume)")
    s = Sim()
    s.run(8, active=None, surr=CLEAR)              # cruise first
    s.approach('stop', surr=CLEAR)                 # decelerate to the sign
    check(s.reached(APPROACHING_SIGN), "entered APPROACHING_SIGN")
    check(s.reached(STOPPED), "reached STOPPED at the sign")

    # While STOPPED the wheels must actually be zero.
    s.run(6, active=None, surr=CLEAR)
    halted = s.trace[-1]
    check(halted['state'] in (STOPPED, WAITING_FOR_RIGHT_OF_WAY),
          "still stopped/holding during the pause")
    check(halted['left'] == 0.0 and halted['right'] == 0.0, "wheels are fully stopped")

    # Speed must have decreased monotonically through the approach (no jerk up).
    approach_speeds = [max(r['left'], r['right']) for r in s.trace
                       if r['state'] == APPROACHING_SIGN]
    non_increasing = all(b <= a + 1e-6 for a, b in zip(approach_speeds, approach_speeds[1:]))
    check(len(approach_speeds) >= 2 and non_increasing,
          "speed decreased smoothly during approach (no acceleration into the sign)")

    # After the pause, with the way clear, it resumes driving.
    s.run(int((s.cfg.stop_pause_s + s.cfg.turn_straight_s) / s.dt) + 10, active=None, surr=CLEAR)
    check(s.reached(WAITING_FOR_RIGHT_OF_WAY), "checked right-of-way after the pause")
    check(s.sm.state == DRIVING, "resumed DRIVING after the stop")
    check(s.smooth_ok, f"motion stayed smooth (worst jump {s.worst_jump:.3f} <= {s.max_step:.3f})")


def test_stage2_cooldown():
    print("\nStage 2b: does not re-trigger on the sign it just passed")
    s = Sim()
    s.approach('stop', surr=CLEAR)
    s.run(int((s.cfg.stop_pause_s + s.cfg.turn_straight_s) / s.dt) + 10, active=None, surr=CLEAR)
    check(s.sm.state == DRIVING, "back to DRIVING")
    # The same stop sign is still in view as we pull away; cooldown must ignore it.
    s.run(10, active=mk_active('stop', 0.45), surr=CLEAR)
    check(s.sm.state == DRIVING, "ignored the just-handled stop sign during cooldown")


def test_stage2_no_restop_while_sign_lingers():
    print("\nStage 2c: no SECOND stop while the same sign lingers in-zone past cooldown")
    s = Sim()
    s.approach('stop', surr=CLEAR)
    s.run(int((s.cfg.stop_pause_s + s.cfg.turn_straight_s) / s.dt) + 10, active=None, surr=CLEAR)
    check(s.sm.state == DRIVING, "resumed driving after the stop")
    # Same sign (tag 0) stays within the reaction zone far longer than cooldown_s
    # as the bot crawls away. The tag-id latch must keep suppressing it.
    s.run(int(s.cfg.cooldown_s / s.dt) + 40, active=mk_active('stop', 0.45, tag_id=0), surr=CLEAR)
    check(s.sm.state == DRIVING, "did not re-stop at the same lingering sign (no re-trigger loop)")


def test_stage2_stop_only_while_line_lingers():
    print("\nStage 2e: stops ONCE (~stop_pause_s), not for as long as the red line shows")
    s = Sim()
    s.approach('stop', surr=CLEAR)                 # first (and only) stop at the line
    # The red stop line + stop sign stay in view far longer than the pause as the
    # bot sits on/rolls over the line. It must NOT keep re-stopping.
    held = Surroundings(stop_line_ahead=True)
    s.run(int(s.cfg.stop_pause_s / s.dt) + 80, active=mk_active('stop', 0.25, tag_id=0), surr=held)
    entries, prev = 0, None
    for r in s.trace:
        if r['state'] == STOPPED and prev != STOPPED:
            entries += 1
        prev = r['state']
    check(entries == 1, f"entered STOPPED exactly once (saw {entries})")
    check(s.sm.state != STOPPED, "not stuck stopped while the red line lingers")


def test_stage2_size_gate_triggers_when_distance_far():
    print("\nStage 2f: reacts to a stop sign via apparent SIZE when distance reads too far")
    # Simulates a mis-scaled distance estimate (wrong intrinsics / tag size): the
    # metric distance never drops below approach_distance_m, but the tag visibly
    # grows in the frame. The size gate must still start the reaction — this is
    # the "detects the stop sign but never acts on it" bug.
    s = Sim()
    far = s.cfg.approach_distance_m + 0.5            # distance gate can never fire
    s.run(4, active=None, surr=CLEAR)
    s.run(3, active=mk_active('stop', far, pixel_size=s.cfg.approach_pixel_size + 5), surr=CLEAR)
    check(s.sm.state == APPROACHING_SIGN,
          f"size gate started the reaction despite a far distance ({s.sm.state})")


def test_stage2_stop_via_size_without_line():
    print("\nStage 2g: stops at a stop sign via apparent size when no red line is painted")
    # Distance sits outside at_sign_distance_m (so the metric 'at sign' never
    # fires) and there is no stop line, but the tag grows past at_sign_pixel_size.
    # The bot must still come to a full stop.
    s = Sim()
    s.run(2, active=None, surr=CLEAR)
    s.run(2, active=mk_active('stop', 0.5, pixel_size=s.cfg.approach_pixel_size + 2), surr=CLEAR)
    check(s.sm.state == APPROACHING_SIGN, "approaching the stop sign")
    s.run(6, active=mk_active('stop', 0.5, pixel_size=s.cfg.at_sign_pixel_size + 5), surr=CLEAR)
    check(s.reached(STOPPED), "came to a full stop via the size gate (no line needed)")


def test_stage2_stop_via_lost_grace():
    print("\nStage 2h: stops in front of a stop sign that slips out of view (no line, lost grace)")
    # A high-mounted stop tag is seen while approaching, then disappears above the
    # camera just as we reach it — no red line, and the distance/size gates never
    # trip (distance stays > at_sign_distance_m, pixel_size 0). The lost-sign grace
    # must bring us to a stop rather than coasting to the much-later failsafe.
    s = Sim()
    s.run(2, active=None, surr=CLEAR)
    s.run(3, active=mk_active('stop', 0.45), surr=CLEAR)   # within approach range, not 'at sign'
    check(s.sm.state == APPROACHING_SIGN, "approaching the stop sign")
    n = int(s.cfg.lost_grace_s / s.dt) + 3                 # sign gone past the grace window
    s.run(n, active=None, surr=CLEAR)
    check(s.reached(STOPPED), "stopped via lost-sign grace (no line, sign left view)")
    # The grace must act well before the line-search failsafe (else it overshoots).
    check(s.cfg.lost_grace_s < s.cfg.line_search_max_s, "grace fires before the failsafe")


def test_stage3_no_blind_turn_without_a_junction():
    print("\nStage 3e: an intersection sign NEVER turns without a real stop line")
    # See the intersection sign (latches a turn) but NO red stop line ever shows.
    # The bot must keep lane-following straight through, not arc into the middle
    # of the road — this is the "turns too early / turns into nothing" bug.
    s = Sim()
    s.run(2, active=mk_active('T-intersection', 0.5, turns=['left', 'right']), surr=CLEAR)
    check(s.sm.state == APPROACHING_SIGN, "latched the intersection and is approaching")
    s.run(int(s.cfg.line_search_max_s / s.dt) + 20, active=None, surr=CLEAR)
    check(not s.reached(TURNING), "never entered TURNING without a junction")
    check(s.sm.state == DRIVING, "fell back to lane following (drove straight through)")
    check(s.smooth_ok, f"motion stayed smooth (worst jump {s.worst_jump:.3f} <= {s.max_step:.3f})")


def test_stage3_force_turn():
    print("\nStage 3d: force_turn overrides the random pick (when allowed)")
    cfg = BehaviorConfig(); cfg.force_turn = "left"
    s = Sim(cfg=cfg)
    s.approach('T-intersection', turns=['left', 'right'], surr=CLEAR)
    check(s.sm.chosen_turn == 'left', f"forced 'left' was chosen ({s.sm.chosen_turn})")
    # If the sign forbids the forced turn, fall back to a valid one.
    cfg2 = BehaviorConfig(); cfg2.force_turn = "left"
    s2 = Sim(cfg=cfg2)
    s2.approach('right-T-intersect', turns=['straight', 'right'], surr=CLEAR)
    check(s2.sm.chosen_turn in ('straight', 'right'),
          f"forbidden forced turn fell back to a valid one ({s2.sm.chosen_turn})")


def test_stage2_new_sign_not_suppressed():
    print("\nStage 2d: a DIFFERENT sign right after one is handled is NOT suppressed")
    s = Sim()
    s.approach('stop', surr=CLEAR)  # handle stop sign tag 0
    s.run(int((s.cfg.stop_pause_s + s.cfg.turn_straight_s) / s.dt) + 10, active=None, surr=CLEAR)
    check(s.sm.state == DRIVING, "resumed after the first stop")
    # A genuinely different sign (tag 99) appears immediately, well within the
    # first sign's cooldown window. It must still be reacted to.
    s.run(3, active=mk_active('stop', 0.50, tag_id=99), surr=CLEAR)
    check(s.sm.state == APPROACHING_SIGN, "reacted to a different sign during the first sign's cooldown")


# ---------------------------------------------------------------------------
# Stage 3 — intersection: random valid turn, executed cleanly
# ---------------------------------------------------------------------------
def test_stage3_random_turn():
    print("\nStage 3: intersection -> random valid turn -> execute -> resume")
    s = Sim()
    s.approach('T-intersection', turns=['left', 'right'], surr=CLEAR)
    check(s.reached(STOPPED), "stopped at the intersection sign")
    check(s.sm.chosen_turn in ('left', 'right'), f"chose a valid turn ({s.sm.chosen_turn})")

    chosen = s.sm.chosen_turn
    # Run the whole manoeuvre: stop pause + right-of-way + intersection entry +
    # the arc itself, with margin.
    total = (s.cfg.stop_pause_s + s.cfg.intersection_entry_s
             + max(s.cfg.turn_left_s, s.cfg.turn_right_s))
    s.run(int(total / s.dt) + 12, active=None, surr=CLEAR)
    check(s.reached(TURNING), "entered TURNING")

    # The bot must first drive straight INTO the intersection (entry phase), then
    # arc the chosen way — so both a straight-entry frame and an arc frame appear.
    turning = [r for r in s.trace if r['state'] == TURNING]
    ev = s.cfg.intersection_entry_speed
    saw_entry = any(abs(r['left'] - ev) < 0.06 and abs(r['right'] - ev) < 0.06 for r in turning)
    want = {'left': (s.cfg.turn_left_left, s.cfg.turn_left_right),
            'right': (s.cfg.turn_right_left, s.cfg.turn_right_right)}[chosen]
    saw_arc = any(abs(r['left'] - want[0]) < 0.12 and abs(r['right'] - want[1]) < 0.12 for r in turning)
    check(saw_entry, "drove straight into the intersection before turning")
    check(saw_arc, f"then arced the configured {chosen} turn")

    check(s.sm.state == DRIVING, "resumed DRIVING after completing the turn")
    check(s.smooth_ok, f"motion stayed smooth (worst jump {s.worst_jump:.3f} <= {s.max_step:.3f})")


def test_stage3_randomness_and_validity():
    print("\nStage 3b: turn choice is random and always valid")
    turns = ['left', 'straight', 'right']
    seen = set()
    all_valid = True
    for seed in range(40):
        s = Sim(seed=seed)
        s.approach('4-way-intersect', turns=turns, surr=CLEAR)
        if s.sm.chosen_turn not in turns:
            all_valid = False
        seen.add(s.sm.chosen_turn)
    check(all_valid, "every chosen turn was in the allowed set")
    check(len(seen) >= 2, f"choice varied across trials (saw {sorted(seen)})")


def test_stage3_only_allowed_turns():
    print("\nStage 3c: never picks a turn the sign forbids")
    ok = True
    for seed in range(40):
        s = Sim(seed=seed)
        s.approach('right-T-intersect', turns=['straight', 'right'], surr=CLEAR)
        if s.sm.chosen_turn == 'left':
            ok = False
    check(ok, "never chose 'left' when only straight/right were allowed")


# ---------------------------------------------------------------------------
# Stage 4 — obstacle stopping
# ---------------------------------------------------------------------------
def test_stage4_obstacle_stop_resume():
    print("\nStage 4: stop for an obstacle, resume when clear")
    s = Sim()
    s.run(8, active=None, surr=CLEAR)
    blocked = Surroundings(obstacle_ahead=True, obstacle_reason='truck in lane')
    s.run(10, active=None, surr=blocked)
    check(s.reached(OBSTACLE_STOP), "entered OBSTACLE_STOP")
    check(s.trace[-1]['left'] == 0.0 and s.trace[-1]['right'] == 0.0,
          "wheels stopped for the obstacle")
    s.run(8, active=None, surr=CLEAR)
    check(s.sm.state == DRIVING, "resumed DRIVING after the obstacle cleared")
    check(s.smooth_ok, f"motion stayed smooth (worst jump {s.worst_jump:.3f} <= {s.max_step:.3f})")


def test_stage4_obstacle_during_approach():
    print("\nStage 4b: obstacle while approaching a sign also stops")
    s = Sim()
    s.tick(mk_active('stop', 0.5), CLEAR)
    check(s.sm.state == APPROACHING_SIGN, "approaching the sign")
    s.run(6, active=mk_active('stop', 0.45),
          surr=Surroundings(obstacle_ahead=True, obstacle_reason='duckie in lane'))
    check(s.sm.state == OBSTACLE_STOP, "obstacle overrides the approach")


# ---------------------------------------------------------------------------
# Stage 5 — two-robot right-of-way ("right has precedence")
# ---------------------------------------------------------------------------
def test_stage5_right_of_way_intersection():
    print("\nStage 5: wait for a robot on the right, then turn")
    s = Sim()
    robot = Surroundings(robot_on_right=True)
    s.approach('T-intersection', turns=['left', 'right'], surr=CLEAR)  # -> STOPPED
    # The robot is on our right while we finish the stop pause; when we move to
    # the right-of-way check we must HOLD rather than proceed.
    s.run(int(s.cfg.stop_pause_s / s.dt) + 20, active=None, surr=robot)
    check(s.sm.state == WAITING_FOR_RIGHT_OF_WAY, "held for the robot on the right")
    check(s.trace[-1]['left'] == 0.0 and s.trace[-1]['right'] == 0.0, "stayed stopped while waiting")
    # It clears: we proceed into the turn.
    s.run(6, active=None, surr=CLEAR)
    check(s.reached(TURNING), "proceeded into the turn once the right was clear")
    check(s.smooth_ok, f"motion stayed smooth (worst jump {s.worst_jump:.3f} <= {s.max_step:.3f})")


def test_stage5_right_of_way_timeout():
    print("\nStage 5c: gives way, but proceeds after the max wait (no deadlock)")
    s = Sim()
    robot = Surroundings(robot_on_right=True)
    s.approach('T-intersection', turns=['left', 'right'], surr=CLEAR)   # -> STOPPED
    s.run(int(s.cfg.stop_pause_s / s.dt) + 2, active=None, surr=robot)  # reach WAITING, robot present
    check(s.sm.state == WAITING_FOR_RIGHT_OF_WAY, "waiting for the robot on the right")
    # The robot never leaves (e.g. a static duckie the detector can't tell apart).
    # After right_of_way_max_wait_s the bot must proceed anyway rather than deadlock.
    s.run(int(s.cfg.right_of_way_max_wait_s / s.dt) + 8, active=None, surr=robot)
    check(s.sm.state in (TURNING, DRIVING), "proceeded after the max wait despite the robot")
    check(s.smooth_ok, f"motion stayed smooth (worst jump {s.worst_jump:.3f} <= {s.max_step:.3f})")


def test_stage5_yield_slows_then_resumes():
    print("\nStage 5b: yield eases off near the sign, never stops, resumes cruise once past")
    s = Sim()
    s.run(12, active=None, surr=CLEAR)                       # reach steady cruise speed
    cruise = max(s.trace[-1]['left'], s.trace[-1]['right'])
    check(cruise > 0.0, "cruising before the yield sign")

    # Yield sign comes into range -> slow down, but keep moving (no stop).
    s.run(10, active=mk_active('yield', 0.45), surr=CLEAR)
    check(s.sm.state == YIELDING, "entered YIELDING for the yield sign")
    slow = max(s.trace[-1]['left'], s.trace[-1]['right'])
    check(0.0 < slow < cruise, f"slowed down but kept moving ({slow:.3f} < {cruise:.3f})")

    # A robot on the right must NOT make a yield halt (yield is give-way, not stop).
    s.run(6, active=mk_active('yield', 0.40), surr=Surroundings(robot_on_right=True))
    check(s.sm.state == YIELDING, "yield does not stop for a robot on the right")

    # Drove past the sign (tag out of view) -> back to normal cruise speed.
    s.run(int(s.cfg.lost_grace_s / s.dt) + 12, active=None, surr=CLEAR)
    check(s.sm.state == DRIVING, "resumed DRIVING after passing the yield sign")
    resumed = max(s.trace[-1]['left'], s.trace[-1]['right'])
    check(resumed > slow + 1e-6, f"returned to normal speed after the yield ({resumed:.3f} > {slow:.3f})")
    check(not s.reached(STOPPED) and not s.reached(WAITING_FOR_RIGHT_OF_WAY),
          "yield never used the stop / right-of-way states")
    check(s.smooth_ok, f"motion stayed smooth (worst jump {s.worst_jump:.3f} <= {s.max_step:.3f})")


# ---------------------------------------------------------------------------
# Smoothness stress test — adversarial target flipping
# ---------------------------------------------------------------------------
def test_smoothness_stress():
    print("\nSmoothness: adversarial target flipping never jerks")
    s = Sim()
    seqs = [
        (None, CLEAR),                                            # cruise
        (None, Surroundings(obstacle_ahead=True)),               # slam stop
        (None, CLEAR),                                            # go
        (mk_active('T-intersection', 0.3, ['left']), CLEAR),     # approach/turn
    ]
    for i in range(200):
        active, surr = seqs[i % len(seqs)]
        s.tick(active, surr)
    check(s.smooth_ok,
          f"no tick exceeded the slew limit (worst jump {s.worst_jump:.3f} <= {s.max_step:.3f})")


def test_smoothness_small_dt():
    print("\nSmoothness: holds at a fast control rate (small dt + snap-to-zero)")
    # dt=0.01 -> max_step=0.02 < snap_zero(0.03): the snap must be capped by the
    # slew limit, else stopping would jerk. Exercises the decel ramp + snap.
    s = Sim(dt=0.01)
    s.run(30, active=None, surr=CLEAR)
    s.approach('stop', surr=CLEAR)
    s.run(80, active=None, surr=CLEAR)
    check(s.smooth_ok,
          f"no tick exceeded the slew limit at dt={s.dt} (worst {s.worst_jump:.4f} <= {s.max_step:.4f})")


# ---------------------------------------------------------------------------
# Lane-follower reset helper
# ---------------------------------------------------------------------------
def test_reset_lane_follower():
    print("\nLane reset: clears stale PD / smoothing state")
    from collections import deque

    class FakeLane:
        def __init__(self):
            self._prev_error = 0.7
            self._filtered_error = -0.4
            self._left_history = deque([0.3, 0.3], maxlen=3)
            self._right_history = deque([0.5], maxlen=3)

    lane = FakeLane()
    reset_lane_follower(lane)
    check(lane._prev_error == 0.0 and lane._filtered_error == 0.0, "errors zeroed")
    check(len(lane._left_history) == 0 and len(lane._right_history) == 0, "history cleared")
    reset_lane_follower(None)  # must not raise
    check(True, "reset_lane_follower(None) is a safe no-op")


def main():
    print("=" * 64)
    print("TRAFFIC SIGNS — BEHAVIOUR STATE-MACHINE SELF-CHECK (no robot)")
    print("=" * 64)

    test_stage1_cruise()
    test_stage2_stop_sign()
    test_stage2_cooldown()
    test_stage2_no_restop_while_sign_lingers()
    test_stage2_stop_only_while_line_lingers()
    test_stage2_new_sign_not_suppressed()
    test_stage2_size_gate_triggers_when_distance_far()
    test_stage2_stop_via_size_without_line()
    test_stage2_stop_via_lost_grace()
    test_stage3_no_blind_turn_without_a_junction()
    test_stage3_force_turn()
    test_stage3_random_turn()
    test_stage3_randomness_and_validity()
    test_stage3_only_allowed_turns()
    test_stage4_obstacle_stop_resume()
    test_stage4_obstacle_during_approach()
    test_stage5_right_of_way_intersection()
    test_stage5_right_of_way_timeout()
    test_stage5_yield_slows_then_resumes()
    test_smoothness_stress()
    test_smoothness_small_dt()
    test_reset_lane_follower()

    print("\n" + "=" * 64)
    if _failures:
        print(f"FAILED: {len(_failures)} check(s) did not pass:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == '__main__':
    sys.exit(main())
