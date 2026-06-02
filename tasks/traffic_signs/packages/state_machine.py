"""Traffic-sign behaviour layer — the "Person B" half of the project.

This module is the decision-making brain that sits on top of the perception
layer (Person A, ``TrafficSignAgent`` + ``select_active_sign``). It is split
into three small, independently-testable pieces so the state logic stays
readable and separate from the low-level motor commands (a grading criterion):

  1. ``TrafficSignStateMachine`` — pure decision logic. Given the perception
     ``active`` dict and a ``Surroundings`` snapshot, it advances an explicit
     state machine and returns a high-level ``DriveCommand`` ("follow the lane
     at 40% speed", "halt", "arc left"). It knows NOTHING about wheels, the
     camera, the lane agent, or OpenCV — so it can be unit-tested with plain
     dicts and no robot (see ``test_state_machine.py``).

  2. ``MotionController`` — turns a ``DriveCommand`` (plus the lane agent's
     raw command) into concrete left/right wheel speeds in [-1, 1], and
     **slew-rate limits** them so motion is always smooth: the wheel command
     can change by at most ``max_accel * dt`` per tick. This is what guarantees
     "no jerky / instant stops".

  3. ``Surroundings`` — the tiny data contract describing what the obstacle /
     other-robot sensor saw this tick (filled in by ``sensors.py``).

The state machine consumes the perception contract documented in
``tasks/traffic_signs/INTERFACE.md`` exactly — it never reaches into the
perception internals.

States (see ``transition table`` in the module docstring of each handler):

    DRIVING                 normal lane following at cruise speed
    APPROACHING_SIGN        a stop/yield/intersection sign is close; decelerate
    STOPPED                 fully stopped at a stop/intersection sign, pausing
    WAITING_FOR_RIGHT_OF_WAY halted, giving way to a robot on the right
    TURNING                 executing a chosen turn through an intersection
    OBSTACLE_STOP           halted because an obstacle is directly ahead
"""

import os
import random
from dataclasses import dataclass

# --- State names (plain strings so they read well in logs / the video overlay).
DRIVING                  = "DRIVING"
APPROACHING_SIGN         = "APPROACHING_SIGN"
STOPPED                  = "STOPPED"
WAITING_FOR_RIGHT_OF_WAY = "WAITING_FOR_RIGHT_OF_WAY"
TURNING                  = "TURNING"
OBSTACLE_STOP            = "OBSTACLE_STOP"

_CONFIG_FILE = os.path.normpath(os.path.join(
    os.path.dirname(__file__), '..', '..', '..', 'config', 'traffic_signs_config.yaml'
))


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------
@dataclass
class Surroundings:
    """What the obstacle / other-robot sensor saw this tick.

    Produced by ``sensors.SurroundingsSensor`` from the object-detection agent.
    Kept deliberately tiny so the state machine has no dependency on the
    (heavy) detector and can be unit-tested with hand-built snapshots.
    """
    obstacle_ahead: bool = False   # something blocking the lane straight ahead
    robot_on_right: bool = False   # another vehicle to our right (right-of-way)
    obstacle_reason: str = ""      # human-readable label for logging/overlay

    @classmethod
    def clear(cls) -> "Surroundings":
        """A 'nothing detected' snapshot — the safe default before the sensor
        has produced anything (or when the detector model is unavailable)."""
        return cls()


@dataclass
class DriveCommand:
    """A high-level driving intent emitted by the state machine.

    ``kind`` is one of:
      'lane_follow' — delegate steering/speed to the lane agent, scaled by
                      ``speed_scale`` in [0, 1] (used to smoothly decelerate).
      'arc'         — drive an open-loop arc with the given ``left``/``right``
                      normalized wheel speeds (used for intersection turns).
      'halt'        — request zero speed (the MotionController still ramps down
                      smoothly to honour the no-jerk rule).
    """
    kind: str
    speed_scale: float = 1.0
    left: float = 0.0
    right: float = 0.0
    note: str = ""


# ---------------------------------------------------------------------------
# Tunables (loaded from config/traffic_signs_config.yaml -> behavior:)
# ---------------------------------------------------------------------------
@dataclass
class BehaviorConfig:
    # --- Sign approach / stop timing -------------------------------------
    approach_distance_m: float = 0.60   # start reacting to a sign at this range
    at_sign_distance_m:  float = 0.30   # "arrived" range (matches perception)
    stop_pause_s:        float = 2.0    # dwell time at a stop sign
    yield_creep_scale:   float = 0.40   # min speed while creeping past a yield
    cruise_scale:        float = 1.0    # lane-follow speed scale when DRIVING
    cooldown_s:          float = 3.0    # min hold on the handled-sign latch
    lost_grace_s:        float = 0.5    # sign vanished near bot -> treat arrived
    right_of_way_max_wait_s: float = 5.0  # give way then proceed (no deadlock)

    # --- Open-loop turns (NORMALIZED wheel speeds + durations) -----------
    # These MUST be calibrated on the real robot / in the target sim. They are
    # deliberately gentle; tune duration to ~90 deg and the L/R split to the
    # turn radius of your track.
    turn_left_left:      float = 0.00
    turn_left_right:     float = 0.32
    turn_left_s:         float = 1.6
    turn_right_left:     float = 0.32
    turn_right_right:    float = 0.00
    turn_right_s:        float = 1.6
    turn_straight_left:  float = 0.28
    turn_straight_right: float = 0.28
    turn_straight_s:     float = 1.2

    # --- Motion smoothing -------------------------------------------------
    max_accel:  float = 2.0   # max change in wheel command per second (slew)
    snap_zero:  float = 0.03  # below this, a zero target snaps to a hard 0

    @classmethod
    def from_yaml(cls, path: str = None) -> "BehaviorConfig":
        # yaml is imported lazily so the pure-logic state machine (and its
        # hardware-free unit test) need only the standard library.
        path = path or _CONFIG_FILE
        try:
            import yaml
            with open(path) as f:
                cfg = (yaml.safe_load(f) or {}).get('behavior', {}) or {}
        except Exception:
            cfg = {}
        out = cls()
        for field in out.__dataclass_fields__:
            if field in cfg:
                setattr(out, field, type(getattr(out, field))(cfg[field]))
        return out


# ---------------------------------------------------------------------------
# The state machine (pure logic — no wheels, no camera)
# ---------------------------------------------------------------------------
class TrafficSignStateMachine:
    """Explicit state machine consuming the perception ``active`` signal.

    Call :meth:`update` once per control tick with:
      * ``active``       — the perception active-sign dict, or ``None``.
      * ``surroundings`` — a :class:`Surroundings` snapshot (or ``None``).
      * ``dt``           — seconds since the previous tick.

    It returns a :class:`DriveCommand`. Feed that, together with the lane
    agent's raw command, to :class:`MotionController` to get wheel speeds.
    """

    def __init__(self, config: BehaviorConfig = None, rng: random.Random = None):
        self.cfg = config or BehaviorConfig.from_yaml()
        # Injectable RNG so tests are reproducible; defaults to the global one.
        self._rng = rng or random.Random()

        self.state = DRIVING
        self.note = ""               # short human-readable status for overlay
        self._now = 0.0              # monotonic-ish clock advanced by dt
        self._state_time = 0.0       # seconds spent in the current state
        self._cooldown_until = 0.0   # minimum hold time on the handled-sign latch
        self._lost_time = 0.0        # how long the target sign has been missing

        self._sign_kind = None       # 'stop' | 'yield' | 'intersection'
        self._chosen_turn = None     # 'left' | 'straight' | 'right'
        self._turn_elapsed = 0.0     # progress through an open-loop turn
        self._last_dist = self.cfg.approach_distance_m

        # Re-trigger suppression keyed on the AprilTag id of the sign we just
        # handled, so we don't stop twice at the same sign while it lingers in
        # view — but a *different* sign is never suppressed.
        self._target_tag = None      # tag_id of the sign currently being handled
        self._handled_tag = None     # tag_id latched after we finish a sign

    # -- public helpers ---------------------------------------------------
    @property
    def chosen_turn(self):
        return self._chosen_turn

    def reset(self):
        """Return to a clean DRIVING state (called when autonomy is (re)started
        via /start or /stop so we never resume mid-manoeuvre)."""
        self.state = DRIVING
        self.note = ""
        self._state_time = 0.0
        self._sign_kind = None
        self._chosen_turn = None
        self._turn_elapsed = 0.0
        self._lost_time = 0.0
        self._target_tag = None
        self._handled_tag = None     # fresh start reacts to whatever is in view

    # -- main tick --------------------------------------------------------
    def update(self, active, surroundings, dt: float) -> DriveCommand:
        self._now += dt
        self._state_time += dt
        surroundings = surroundings or Surroundings.clear()

        handler = {
            DRIVING:                  self._h_driving,
            APPROACHING_SIGN:         self._h_approaching,
            STOPPED:                  self._h_stopped,
            WAITING_FOR_RIGHT_OF_WAY: self._h_waiting,
            TURNING:                  self._h_turning,
            OBSTACLE_STOP:            self._h_obstacle,
        }[self.state]

        cmd = handler(active, surroundings, dt)
        cmd.note = self.note
        return cmd

    # -- state handlers ---------------------------------------------------
    # DRIVING: cruise on the lane. Watch for (a) an obstacle dead ahead and
    # (b) a sign close enough to act on. Signs are ignored during the
    # post-manoeuvre cooldown so we don't re-trigger on the sign we just passed.
    def _h_driving(self, active, surr, dt) -> DriveCommand:
        if surr.obstacle_ahead:
            return self._goto(OBSTACLE_STOP, f"obstacle: {surr.obstacle_reason}",
                              self._cmd_halt())

        self._update_sign_latch(active)
        if active is not None and not self._suppressed(active) \
                and active['distance_m'] <= self.cfg.approach_distance_m:
            kind = self._classify(active)
            if kind is not None:
                self._sign_kind = kind
                self._target_tag = active['tag_id']
                self._chosen_turn = (self._rng.choice(active['turns'])
                                     if kind == 'intersection' else None)
                self._lost_time = 0.0
                self._last_dist = active['distance_m']
                turn_txt = f" -> turn {self._chosen_turn}" if self._chosen_turn else ""
                return self._goto(APPROACHING_SIGN, f"approaching {kind}{turn_txt}",
                                  self._cmd_approach(active))

        self.note = "driving"
        return self._cmd_lane(self.cfg.cruise_scale)

    # APPROACHING_SIGN: decelerate smoothly while still steering with the lane
    # agent. We slow in proportion to the remaining distance so the bot eases
    # to a halt right at the sign rather than braking suddenly. When the sign
    # is reached (or vanishes from view because we're right on top of it),
    # branch by sign type.
    def _h_approaching(self, active, surr, dt) -> DriveCommand:
        if surr.obstacle_ahead:
            return self._goto(OBSTACLE_STOP, f"obstacle: {surr.obstacle_reason}",
                              self._cmd_halt())

        if active is not None:
            self._lost_time = 0.0
            self._last_dist = active['distance_m']
            arrived = active['at_sign'] or active['distance_m'] <= self.cfg.at_sign_distance_m
            scale = self._approach_scale(active['distance_m'])
        else:
            # Sign dropped out of view. Near a sign this means it slipped above
            # the camera, so keep decelerating and treat as arrived after a
            # short grace (also rides out 1-frame perception dropouts).
            self._lost_time += dt
            arrived = self._lost_time >= self.cfg.lost_grace_s
            scale = 0.0

        if arrived:
            return self._on_arrival()

        return self._cmd_lane(scale)

    # STOPPED: full stop at a stop/intersection sign. Hold zero speed for the
    # configured pause, then go check right-of-way before proceeding.
    def _h_stopped(self, active, surr, dt) -> DriveCommand:
        if self._state_time >= self.cfg.stop_pause_s:
            return self._goto(WAITING_FOR_RIGHT_OF_WAY, "checking right-of-way",
                              self._cmd_halt())
        self.note = f"stopped ({self._state_time:.1f}/{self.cfg.stop_pause_s:.1f}s)"
        return self._cmd_halt()

    # WAITING_FOR_RIGHT_OF_WAY: "right has precedence". Stay halted while a
    # robot is detected to our right (or an obstacle blocks the box). Once
    # clear, either execute the chosen turn (intersection) or resume driving.
    def _h_waiting(self, active, surr, dt) -> DriveCommand:
        # Right-of-way is "give way", not "block forever": after a bounded wait
        # we proceed even if the right still looks occupied (e.g. a parked duck
        # the detector can't distinguish from a robot). An obstacle directly
        # ahead always holds us — we never drive into a confirmed obstacle.
        waited_out = self._state_time >= self.cfg.right_of_way_max_wait_s
        if surr.robot_on_right and not waited_out:
            self.note = "waiting: robot on right has priority"
            return self._cmd_halt()
        if surr.obstacle_ahead:
            self.note = "waiting: obstacle ahead"
            return self._cmd_halt()

        if self._chosen_turn is not None:
            self._turn_elapsed = 0.0
            return self._goto(TURNING, f"turning {self._chosen_turn}",
                              self._cmd_turn())
        self._mark_handled()
        return self._goto(DRIVING, "proceeding", self._cmd_lane(self.cfg.cruise_scale))

    # TURNING: open-loop arc for a fixed duration. We do NOT lane-follow here
    # because lane markings through an intersection are ambiguous. If an
    # obstacle appears mid-turn we pause (halt) without consuming turn time, so
    # the manoeuvre resumes cleanly once the way is clear.
    def _h_turning(self, active, surr, dt) -> DriveCommand:
        left, right, duration = self._turn_params()

        if surr.obstacle_ahead:
            self.note = f"turn {self._chosen_turn} paused: obstacle"
            return self._cmd_halt()

        self._turn_elapsed += dt
        if self._turn_elapsed >= duration:
            self._chosen_turn = None
            self._mark_handled()
            return self._goto(DRIVING, "turn complete", self._cmd_lane(self.cfg.cruise_scale))

        self.note = f"turning {self._chosen_turn} ({self._turn_elapsed:.1f}/{duration:.1f}s)"
        return self._cmd_arc(left, right)

    # OBSTACLE_STOP: stay halted until the obstacle clears (the sensor applies
    # its own hysteresis so this doesn't flap), then resume driving.
    def _h_obstacle(self, active, surr, dt) -> DriveCommand:
        if not surr.obstacle_ahead:
            return self._goto(DRIVING, "obstacle cleared", self._cmd_lane(self.cfg.cruise_scale))
        self.note = f"obstacle: {surr.obstacle_reason or 'blocked'}"
        return self._cmd_halt()

    # -- arrival branching ------------------------------------------------
    def _on_arrival(self) -> DriveCommand:
        """Reached the sign during APPROACHING_SIGN; pick the next state."""
        if self._sign_kind in ('stop', 'intersection'):
            return self._goto(STOPPED, "stopping", self._cmd_halt())
        # yield: no full stop required — go straight to the right-of-way check,
        # which proceeds immediately if the way is clear.
        return self._goto(WAITING_FOR_RIGHT_OF_WAY, "yield: checking", self._cmd_halt())

    # -- small helpers ----------------------------------------------------
    def _classify(self, active):
        """Map a perception active-sign dict to the behaviour we owe it, or
        None if it's an informational sign we don't act on."""
        if active.get('turns'):
            return 'intersection'
        if active.get('sign_type') == 'stop':
            return 'stop'
        if active.get('sign_type') == 'yield':
            return 'yield'
        return None  # t-light-ahead, pedestrian, parking, ... -> just drive

    def _approach_scale(self, distance_m) -> float:
        """Linear speed ramp: 1.0 at approach_distance down to 0.0 at the sign.
        Yield signs keep a creep floor instead of stopping."""
        a, b = self.cfg.approach_distance_m, self.cfg.at_sign_distance_m
        scale = (distance_m - b) / max(1e-3, (a - b))
        scale = max(0.0, min(1.0, scale))
        if self._sign_kind == 'yield':
            scale = max(scale, self.cfg.yield_creep_scale)
        return scale

    def _turn_params(self):
        c = self.cfg
        return {
            'left':     (c.turn_left_left,     c.turn_left_right,     c.turn_left_s),
            'right':    (c.turn_right_left,    c.turn_right_right,    c.turn_right_s),
            'straight': (c.turn_straight_left, c.turn_straight_right, c.turn_straight_s),
        }[self._chosen_turn]

    def _mark_handled(self):
        """Latch the sign we just finished so we don't immediately re-trigger on
        it while it lingers in view as we pull away. ``cooldown_s`` is a *minimum*
        hold (rides out 1-frame perception dropouts); the latch then clears once
        the sign actually leaves the reaction zone (see _update_sign_latch)."""
        self._handled_tag = self._target_tag
        self._cooldown_until = self._now + self.cfg.cooldown_s

    def _suppressed(self, active) -> bool:
        """True while ``active`` is the SAME sign we just handled. A different
        sign (different tag_id) is never suppressed, so two distinct signs in a
        cluster (e.g. a stop then a turn-option sign) are both handled."""
        return self._handled_tag is not None and active['tag_id'] == self._handled_tag

    def _update_sign_latch(self, active):
        """Release the handled-sign latch once the minimum hold has elapsed AND
        the sign has left the reaction zone (out of view, a different tag, or
        receded past approach range)."""
        if self._handled_tag is None or self._now < self._cooldown_until:
            return
        if (active is None
                or active['tag_id'] != self._handled_tag
                or active['distance_m'] > self.cfg.approach_distance_m):
            self._handled_tag = None

    def _goto(self, state, note, cmd) -> DriveCommand:
        self.state = state
        self.note = note
        self._state_time = 0.0
        return cmd

    # -- command builders -------------------------------------------------
    def _cmd_lane(self, scale):  return DriveCommand('lane_follow', speed_scale=scale)
    def _cmd_halt(self):         return DriveCommand('halt')
    def _cmd_arc(self, l, r):    return DriveCommand('arc', left=l, right=r)

    def _cmd_turn(self):
        left, right, _duration = self._turn_params()
        return self._cmd_arc(left, right)

    def _cmd_approach(self, active):
        return self._cmd_lane(self._approach_scale(active['distance_m']))


# ---------------------------------------------------------------------------
# Motion controller (the low-level, smoothness-guaranteeing layer)
# ---------------------------------------------------------------------------
class MotionController:
    """Converts a :class:`DriveCommand` into smooth left/right wheel speeds.

    The single job that makes motion smooth: **slew-rate limiting**. No matter
    how abruptly the state machine changes its mind (cruise -> halt -> arc),
    the wheel command moves toward the new target by at most ``max_accel * dt``
    per tick. That mathematically bounds the jerk and is what the smoothness
    unit test checks.
    """

    def __init__(self, config: BehaviorConfig = None):
        self.cfg = config or BehaviorConfig.from_yaml()
        self._left = 0.0
        self._right = 0.0

    def reset(self):
        """Snap the controller to a hard stop (used on /stop and mode changes so
        the next ramp starts from zero rather than the last commanded speed)."""
        self._left = 0.0
        self._right = 0.0

    @property
    def command(self):
        return self._left, self._right

    def step(self, cmd: DriveCommand, lane_command, dt: float):
        """Return the (left, right) wheel speeds to apply this tick.

        ``lane_command`` is the lane agent's raw (left, right) in [0, 1]; it is
        only used when ``cmd.kind == 'lane_follow'``.
        """
        target_l, target_r = self._target(cmd, lane_command)

        max_step = self.cfg.max_accel * max(1e-3, dt)
        # Snap-to-zero is checked on the PRE-slew value and is mutually exclusive
        # with slewing, so the snap and the slew never stack — the per-tick change
        # can never exceed max_step (= max_accel*dt) at any frame rate. snap_zero
        # lets the bot settle to a true 0 instead of dribbling below the deadzone.
        snap = min(self.cfg.snap_zero, max_step)
        self._left  = self._advance(self._left,  target_l, max_step, snap)
        self._right = self._advance(self._right, target_r, max_step, snap)
        return self._left, self._right

    @staticmethod
    def _advance(current, target, max_step, snap):
        if target == 0.0 and abs(current) <= snap:
            return 0.0
        return _clamp(_approach(current, target, max_step), -1.0, 1.0)

    def _target(self, cmd: DriveCommand, lane_command):
        if cmd.kind == 'lane_follow':
            ll = _clamp(lane_command[0], 0.0, 1.0) * cmd.speed_scale
            lr = _clamp(lane_command[1], 0.0, 1.0) * cmd.speed_scale
            return ll, lr
        if cmd.kind == 'arc':
            return cmd.left, cmd.right
        return 0.0, 0.0  # halt


def reset_lane_follower(lane_agent):
    """Clear the lane agent's PD / smoothing state.

    The lane agent low-pass filters its lateral error and keeps a short command
    history across frames. After a stop or a turn that state is stale; resuming
    lane following with it causes a one-off steering burst. Call this on the
    transition back into DRIVING. Safe to call on any object (no-ops on missing
    attributes)."""
    if lane_agent is None:
        return
    for attr, val in (('_prev_error', 0.0), ('_filtered_error', 0.0)):
        if hasattr(lane_agent, attr):
            setattr(lane_agent, attr, val)
    for attr in ('_left_history', '_right_history'):
        hist = getattr(lane_agent, attr, None)
        if hist is not None and hasattr(hist, 'clear'):
            hist.clear()


def _approach(current, target, max_step):
    """Move ``current`` toward ``target`` by at most ``max_step``."""
    delta = target - current
    if delta > max_step:
        return current + max_step
    if delta < -max_step:
        return current - max_step
    return target


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x
