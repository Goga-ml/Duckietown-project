"""Surroundings sensing for the traffic-sign behaviour layer.

The AprilTag perception layer only sees signs, so the state machine's two
"is the way clear?" questions are answered here instead, by reusing the
**object_detection** YOLO agent (the project's purpose-built obstacle detector):

  * obstacle_ahead  — something (duckie/truck) blocking the lane straight ahead.
                      Delegated to object_detection's proven ``should_stop()``
                      so we inherit its tuned enter/exit hysteresis verbatim.
  * robot_on_right  — another vehicle to our right, for the "right has
                      precedence" rule. The detector has no 'robot' class, so we
                      treat any detected vehicle (duckie/truck) sitting in the
                      right region of the frame as the other robot, with our own
                      small hysteresis so the WAITING state doesn't flap.

Both work in the detector's square ``img_size`` pixel space; we feed it a
pre-resized square frame exactly as ``servers/object_detection/real_server.py``
does, so ``should_stop`` sees the coordinates it expects.

If the detection model is unavailable (``best.onnx`` is git-ignored and is NOT
shipped by ``traffic_signs --run`` — make sure it exists at
``tasks/object_detection/models/best.onnx`` on the bot), this sensor degrades
gracefully to "all clear": the sign behaviours (stop/yield/turns) keep working,
and obstacle-stopping / right-of-way are simply inert until the model is present.
"""

import os

import cv2

from tasks.traffic_signs.packages.state_machine import Surroundings
# should_stop is a small, dependency-light helper (no cv2/numpy/onnx of its own),
# so importing it at module load is cheap. The heavy ObjectDetectionAgent (which
# loads the model) is still imported lazily in __init__, so importing this module
# never triggers a model load. NOTE: this is a cross-task import — the
# object_detection package reaches the bot via `git pull` (the whole repo), not
# via `traffic_signs --run`; real_server.py guards this import so its absence
# degrades to "no obstacle/right-of-way" rather than crashing.
from tasks.object_detection.packages.stop_activity import should_stop

_CONFIG_FILE = os.path.normpath(os.path.join(
    os.path.dirname(__file__), '..', '..', '..', 'config', 'traffic_signs_config.yaml'
))


def _load_behavior_cfg(path=None):
    path = path or _CONFIG_FILE
    try:
        import yaml
        with open(path) as f:
            return (yaml.safe_load(f) or {}).get('behavior', {}) or {}
    except Exception:
        return {}


class SurroundingsSensor:
    """Turns camera frames into :class:`Surroundings` snapshots."""

    def __init__(self, agent=None, config=None):
        # Import the detector lazily so importing this module never forces a
        # model load unless a sensor is actually constructed.
        if agent is None:
            from tasks.object_detection.packages.agent import ObjectDetectionAgent
            agent = ObjectDetectionAgent()
        self.agent = agent

        cfg = config if config is not None else _load_behavior_cfg()
        self.min_cx        = float(cfg.get('right_of_way_min_cx', 0.55))
        self.max_cx        = float(cfg.get('right_of_way_max_cx', 1.00))
        self.min_area      = float(cfg.get('right_of_way_min_area', 0.010))
        self.bottom_min    = float(cfg.get('right_of_way_bottom_min', 0.25))
        self.frames_to_set = int(cfg.get('right_of_way_frames_to_set', 2))
        self.frames_to_clr = int(cfg.get('right_of_way_frames_to_clear', 4))

        # Hysteresis state for the right-of-way check.
        self._robot_present = False
        self._set_streak = 0
        self._clear_streak = 0
        self._last = Surroundings.clear()

    @property
    def model_loaded(self) -> bool:
        return bool(getattr(self.agent, 'model_loaded', False))

    def reset(self):
        """Clear all hysteresis so a restart (/start, /set_mode auto) begins from
        'all clear'. Also resets the reused object_detection obstacle state
        machine, which keeps its own module-level state."""
        self._robot_present = False
        self._set_streak = 0
        self._clear_streak = 0
        self._last = Surroundings.clear()
        try:
            import tasks.object_detection.packages.stop_activity as sa
            sa._state = sa._STATE_FOLLOWING
            sa._clear_streak = 0
        except Exception:
            pass

    def update(self, frame_rgb) -> Surroundings:
        """Run detection on one RGB frame and return the latest snapshot.

        On a frame the detector intentionally skips (returns ``None``) we keep
        the previous snapshot rather than spuriously reporting "all clear". Any
        unexpected error keeps the last snapshot too, so a single bad frame can
        never kill the sensing thread."""
        if not self.model_loaded:
            return Surroundings.clear()

        try:
            size = int(self.agent.img_size)
            square = cv2.resize(frame_rgb, (size, size))
            detections = self.agent.detect(square)
            if detections is None:        # skipped frame -> keep last reading
                return self._last

            # Reuse object_detection's obstacle-ahead state machine as-is.
            obstacle_ahead, reason = should_stop(detections, size)
            robot_on_right = self._update_robot_on_right(detections, size)

            self._last = Surroundings(
                obstacle_ahead=obstacle_ahead,
                robot_on_right=robot_on_right,
                obstacle_reason=reason,
            )
            return self._last
        except Exception as e:
            print(f'[Surroundings] update error ({e}); keeping last reading')
            return self._last

    # -- right-of-way ("robot on my right") -------------------------------
    def _update_robot_on_right(self, detections, size) -> bool:
        seen = any(self._in_right_zone(bbox, size) for bbox, _score, _cls in detections)

        if seen:
            self._set_streak += 1
            self._clear_streak = 0
        else:
            self._clear_streak += 1
            self._set_streak = 0

        if not self._robot_present and self._set_streak >= self.frames_to_set:
            self._robot_present = True
        elif self._robot_present and self._clear_streak >= self.frames_to_clr:
            self._robot_present = False
        return self._robot_present

    def _in_right_zone(self, bbox, size) -> bool:
        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) * 0.5
        area = max(0, (x2 - x1)) * max(0, (y2 - y1))
        frame_area = size * size

        if not (self.min_cx * size <= cx <= self.max_cx * size):
            return False
        if area / frame_area < self.min_area:
            return False
        if y2 < self.bottom_min * size:
            return False
        return True
