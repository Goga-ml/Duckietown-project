import sys
import os
import signal
import threading
import time
import queue
import socket

script_dir   = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.join(script_dir, '..', '..')
sys.path.insert(0, project_root)

import cv2
from flask import Flask, Response, render_template_string, jsonify, request

from dataclasses import replace as _dc_replace
from tasks.visual_lane_servoing.packages.agent import LaneServoingAgent
from tasks.traffic_signs.packages.agent import TrafficSignAgent
from tasks.traffic_signs.packages import detection_activity as student
from tasks.traffic_signs.packages.state_machine import (
    TrafficSignStateMachine, MotionController, BehaviorConfig,
    Surroundings, reset_lane_follower, DRIVING, APPROACHING_SIGN,
)
from tasks.traffic_signs.packages.stop_line import StopLineDetector
try:
    from tasks.traffic_signs.packages.sensors import SurroundingsSensor
except Exception as _sensor_import_err:   # e.g. object_detection package absent
    SurroundingsSensor = None
    print('[Init] Surroundings sensor unavailable '
          f'({_sensor_import_err}); obstacle-stop & right-of-way DISABLED, '
          'sign behaviours still active.')
from servers.traffic_signs.visualization import (
    draw_signs, draw_active_banner, draw_status_overlay, draw_behavior_state,
    draw_lane_overlay, draw_stop_line,
)
from servers.templates.traffic_signs import TRAFFIC_SIGNS_TEMPLATE as HTML_TEMPLATE

from duckiebot.camera_driver import CameraDriver
from duckiebot.wheel_driver import DaguWheelsDriver
from duckiebot.wheel_driver.wheels_driver_abs import WheelPWMConfiguration
from launcher.ports import find_available_port
from servers.common import make_frame_generator, shutdown_cleanup, suppress_http_logs


app        = Flask(__name__)
lane_agent = None
sign_agent = None
camera     = None
wheels     = None
running    = False
manual_mode = False
stop_event = threading.Event()

_frame_queue     = queue.Queue(maxsize=1)
_last_detections = []
_active_sign     = None
_detection_lock  = threading.Lock()

# --- Behaviour layer (Person B): state machine + smooth motion + sensing ---
sign_sm          = None                 # TrafficSignStateMachine (decision logic)
motion           = None                 # MotionController (smooth wheel speeds)
surround_sensor  = None                 # SurroundingsSensor (obstacle / robot-on-right)
stop_line_det    = StopLineDetector()   # red stop-line detector (no heavy deps)

_obstacle_queue    = queue.Queue(maxsize=1)
_surroundings      = Surroundings.clear()
_surroundings_lock = threading.Lock()
_stop_line_ahead   = False              # last red-line reading (for /status)

_last_tick_t     = None                 # monotonic time of the previous control tick
_prev_state      = DRIVING              # to detect resume edges and reset lane PD
_control_lock    = threading.Lock()     # serialises the per-tick control + wheel writes

keys_pressed      = {'up': False, 'down': False, 'left': False, 'right': False}
_keys_lock        = threading.Lock()
_keys_last_update = time.time()


def manual_control_loop():
    global _keys_last_update
    while not stop_event.is_set():
        if not manual_mode or not wheels:
            time.sleep(0.05)
            continue
        if time.time() - _keys_last_update > 0.5:
            with _keys_lock:
                for k in keys_pressed:
                    keys_pressed[k] = False
        with _keys_lock:
            kc = keys_pressed.copy()

        left = right = 0.0
        if kc['up']:   left, right = 0.5, 0.5
        if kc['down']: left, right = -0.5, -0.5
        if kc['up'] and kc['left']:    left, right = 0.2, 0.5
        elif kc['up'] and kc['right']: left, right = 0.5, 0.2
        elif kc['left']:               left, right = -0.3, 0.3
        elif kc['right']:              left, right = 0.3, -0.3

        with _control_lock:
            if manual_mode:           # re-check: /set_mode may have switched us
                wheels.set_wheels_speed(left, right)
        time.sleep(0.05)


def detection_loop():
    global _last_detections, _active_sign
    while not stop_event.is_set():
        if sign_agent is None:
            time.sleep(0.1)
            continue
        try:
            frame_rgb = _frame_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        result = sign_agent.detect(frame_rgb)
        if result is not None:
            active = student.select_active_sign(result)
            with _detection_lock:
                _last_detections = result
                _active_sign     = active


def obstacle_loop():
    """Background detection of obstacles / other robots, mirroring the sign
    detection_loop. Runs the (heavy) object-detection model off the video
    thread and publishes the latest Surroundings snapshot."""
    global _surroundings
    while not stop_event.is_set():
        if surround_sensor is None or not surround_sensor.model_loaded:
            time.sleep(0.1)
            continue
        try:
            frame_rgb = _obstacle_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        surr = surround_sensor.update(frame_rgb)
        with _surroundings_lock:
            _surroundings = surr


def visualize(frame_bgr):
    global _last_tick_t, _prev_state, _stop_line_ahead

    if wheels is None:
        return draw_status_overlay(frame_bgr, 'Initializing...')

    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

    # Hand frames to the two async detectors (lossy queues: newest frame wins).
    if sign_agent is not None:
        try:
            _frame_queue.put_nowait(frame_rgb)
        except queue.Full:
            pass
    if surround_sensor is not None and surround_sensor.model_loaded:
        try:
            _obstacle_queue.put_nowait(frame_rgb)
        except queue.Full:
            pass

    # Red stop-line detection runs inline (cheap) for low-latency turn timing.
    _stop_line_ahead = stop_line_det.detect(frame_rgb)

    with _detection_lock:
        detections = list(_last_detections)
        active     = _active_sign
    with _surroundings_lock:
        surr = _surroundings
    surr = _dc_replace(surr, stop_line_ahead=_stop_line_ahead)

    lane_dbg = None
    if manual_mode:
        pass  # the manual_control_loop owns the wheels in manual mode
    elif lane_agent is not None and sign_sm is not None and motion is not None:
        # Serialise the whole control section so a second /video stream or a
        # route thread can't interleave dt / state / wheel writes.
        with _control_lock:
            # Time since the last control tick (clamped so a stalled feed can't
            # produce a huge step that defeats the smooth-motion slew limiter).
            now = time.monotonic()
            dt  = (now - _last_tick_t) if _last_tick_t is not None else 0.05
            _last_tick_t = now
            dt = max(0.01, min(0.2, dt))

            # Always run the lane follower so the overlay can show the lane
            # points even before /start; only wheel output is gated on `running`.
            lane_cmd = lane_agent.compute_commands(frame_rgb)
            lane_dbg = lane_agent.last_debug_info

            if not running:
                wheels.set_wheels_speed(0.0, 0.0)  # held stopped until /start
            else:
                cmd = sign_sm.update(active, surr, dt)
                # On the edge back into DRIVING (from a stop/turn/obstacle), clear
                # the lane agent's stale PD state so it doesn't lurch.
                if sign_sm.state == DRIVING and _prev_state not in (DRIVING, APPROACHING_SIGN):
                    reset_lane_follower(lane_agent)
                _prev_state = sign_sm.state
                if cmd.kind == 'lane_follow':
                    # Plain lane following: drive exactly like the
                    # visual_lane_servoing task — apply the lane agent's wheel
                    # commands directly (no slew limiting) for crisp steering.
                    # Keep the motion controller synced so a later halt/turn
                    # still ramps down smoothly from the real speed.
                    import numpy as _np
                    left  = float(_np.clip(lane_cmd[0] * cmd.speed_scale, 0.0, 1.0))
                    right = float(_np.clip(lane_cmd[1] * cmd.speed_scale, 0.0, 1.0))
                    motion.sync(left, right)
                else:
                    left, right = motion.step(cmd, lane_cmd, dt)
                wheels.set_wheels_speed(left, right)

    if lane_dbg is not None:
        draw_lane_overlay(frame_bgr, lane_dbg)
    draw_stop_line(frame_bgr, stop_line_det)
    if detections:
        draw_signs(frame_bgr, detections)
    draw_active_banner(frame_bgr, active)
    if sign_sm is not None:
        draw_behavior_state(frame_bgr, sign_sm.state, sign_sm.note)
    return frame_bgr


generate_frames = make_frame_generator(lambda: camera, visualize, quality=50, rgb=False)


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, config=sign_agent, hostname=socket.gethostname(), virtual=False)

@app.route('/video')
def video():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

def _reset_behavior():
    """Return the behaviour layer to a clean DRIVING-from-rest state so we never
    resume mid-manoeuvre and the next launch ramps up smoothly from zero. Always
    call this while holding _control_lock (it touches the shared snapshot)."""
    global _prev_state, _last_tick_t, _surroundings
    if sign_sm is not None:
        sign_sm.reset()
    if motion is not None:
        motion.reset()
    if surround_sensor is not None:
        surround_sensor.reset()
    reset_lane_follower(lane_agent)
    with _surroundings_lock:
        _surroundings = Surroundings.clear()   # don't gate the first tick on stale data
    _prev_state  = DRIVING
    _last_tick_t = None


@app.route('/start', methods=['POST'])
def start():
    global running
    with _control_lock:
        _reset_behavior()
        running = True
    return jsonify({'status': 'running'})

@app.route('/stop', methods=['POST'])
def stop():
    global running
    with _control_lock:
        running = False
        if wheels:
            wheels.set_wheels_speed(0.0, 0.0)
        _reset_behavior()
    return jsonify({'status': 'stopped'})

@app.route('/set_mode', methods=['POST'])
def set_mode():
    global manual_mode
    mode = request.json.get('mode', 'auto') if request.json else 'auto'
    with _control_lock:
        manual_mode = (mode == 'manual')
        if wheels and not manual_mode:
            wheels.set_wheels_speed(0.0, 0.0)
            _reset_behavior()
    return jsonify({'mode': 'manual' if manual_mode else 'auto'})

@app.route('/keys', methods=['POST'])
def update_keys():
    global _keys_last_update
    data = request.json or {}
    with _keys_lock:
        for k in keys_pressed:
            keys_pressed[k] = bool(data.get(k, False))
    _keys_last_update = time.time()
    return jsonify({'status': 'ok'})

@app.route('/set_speed', methods=['POST'])
def set_speed():
    """Live cruise-speed adjuster: sets the lane follower's base/curve speed
    (normalized wheel speed, 0..1). Takes effect on the next frame."""
    value = request.json.get('value') if request.json else None
    if lane_agent is not None and value is not None:
        v = max(0.0, min(1.0, float(value)))
        lane_agent.base_speed = v
        lane_agent.curve_speed = v
    return jsonify({'base_speed': getattr(lane_agent, 'base_speed', None)})

@app.route('/status')
def status():
    with _detection_lock:
        dets = list(_last_detections)
        active = _active_sign
    with _surroundings_lock:
        surr = _surroundings
    return jsonify({
        'running':        running,
        'manual_mode':    manual_mode,
        'detector_ready': sign_agent is not None and sign_agent.load_error is None,
        'load_error':     sign_agent.load_error if sign_agent else None,
        'family':         sign_agent.family if sign_agent else None,
        'base_speed':     getattr(lane_agent, 'base_speed', None) if lane_agent else None,
        'active_sign':    active,
        # Behaviour state machine (Person B).
        'behavior_state': sign_sm.state if sign_sm else None,
        'behavior_note':  sign_sm.note if sign_sm else None,
        'chosen_turn':    sign_sm.chosen_turn if sign_sm else None,
        'obstacle_ahead': surr.obstacle_ahead,
        'robot_on_right': surr.robot_on_right,
        'stop_line':      _stop_line_ahead,
        'sensor_ready':   surround_sensor is not None and surround_sensor.model_loaded,
        'detections': [
            {'tag_id': d.tag_id, 'sign_type': d.sign_type, 'distance_m': d.distance_m,
             'turns': d.turns, 'offset_norm': d.offset_norm}
            for d in dets
        ],
    })


def main():
    global lane_agent, sign_agent, camera, wheels, sign_sm, motion

    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=5000)
    args = ap.parse_args()

    suppress_http_logs()
    print('=' * 60)
    print('TRAFFIC SIGNS — LANE FOLLOW + SIGN BEHAVIOUR STATE MACHINE')
    print('=' * 60)

    # Behaviour layer: lightweight, no hardware deps, so build it up-front.
    beh_cfg = BehaviorConfig.from_yaml()
    sign_sm = TrafficSignStateMachine(beh_cfg)
    motion  = MotionController(beh_cfg)

    def _init_wheels():
        global wheels
        wheels = DaguWheelsDriver(WheelPWMConfiguration(), WheelPWMConfiguration())
        print('[Init] Wheels ready')

    def _init_camera():
        global camera
        cam = CameraDriver()
        cam.start()
        camera = cam
        print('[Init] Camera ready')

    def _init_agents():
        global lane_agent, sign_agent, surround_sensor
        lane_agent = LaneServoingAgent()
        print(f'[Init] Lane agent ready (speed={lane_agent.base_speed})')
        sign_agent = TrafficSignAgent()
        print(f'[Init] Sign agent ready (family={sign_agent.family})')
        # Obstacle / right-of-way sensing reuses the object-detection model.
        # If the module or model is missing the sensor stays absent / reports
        # "all clear" and the sign behaviours still work — only obstacle-stop &
        # right-of-way go inert.
        if SurroundingsSensor is None:
            print('[Init] Surroundings sensor module unavailable '
                  '-> obstacle-stop & right-of-way DISABLED (signs still active).')
        else:
            try:
                surround_sensor = SurroundingsSensor()
                if surround_sensor.model_loaded:
                    print('[Init] Surroundings sensor ready (obstacle + right-of-way)')
                else:
                    print('[Init] Surroundings sensor: object-detection model unavailable '
                          '-> obstacle-stop & right-of-way DISABLED (signs still active). '
                          'Place best.onnx at tasks/object_detection/models/ to enable.')
            except Exception as e:
                print(f'[Init] Surroundings sensor init failed: {e}')

    threading.Thread(target=_init_wheels,        daemon=True).start()
    threading.Thread(target=_init_camera,        daemon=True).start()
    threading.Thread(target=_init_agents,        daemon=True).start()
    threading.Thread(target=detection_loop,      daemon=True).start()
    threading.Thread(target=obstacle_loop,       daemon=True).start()
    threading.Thread(target=manual_control_loop, daemon=True).start()

    def _shutdown(signum, frame):
        shutdown_cleanup(wheels, camera, stop_event)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    web_port = find_available_port(args.port)
    print(f'\nWeb Interface: http://{socket.gethostname()}.local:{web_port}')
    print('=' * 60 + '\n')

    try:
        app.run(host='0.0.0.0', port=web_port, debug=False, threaded=True)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        shutdown_cleanup(wheels, camera, stop_event)


if __name__ == '__main__':
    sys.exit(main())
