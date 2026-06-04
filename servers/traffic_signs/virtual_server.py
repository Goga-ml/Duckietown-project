import sys
import os
import threading
import time
import queue
import socket

script_dir   = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.join(script_dir, '..', '..')
sys.path.insert(0, project_root)

import cv2
from dataclasses import replace as _dc_replace
from flask import Flask, Response, render_template_string, jsonify, request

from tasks.visual_lane_servoing.packages.agent import LaneServoingAgent
from tasks.traffic_signs.packages.agent import TrafficSignAgent
from tasks.traffic_signs.packages import detection_activity as student
from tasks.traffic_signs.packages.state_machine import (
    TrafficSignStateMachine, MotionController, BehaviorConfig,
    Surroundings, reset_lane_follower, DRIVING, APPROACHING_SIGN, YIELDING,
)
from tasks.traffic_signs.packages.stop_line import StopLineDetector
try:
    from tasks.traffic_signs.packages.sensors import SurroundingsSensor
except Exception as _sensor_import_err:
    SurroundingsSensor = None
    print(f'[Init] Surroundings sensor unavailable ({_sensor_import_err}); '
          'obstacle-stop & right-of-way DISABLED, sign behaviours still active.')
from servers.traffic_signs.visualization import (
    draw_signs, draw_active_banner, draw_lane_overlay, draw_behavior_state, draw_stop_line,
)
from servers.templates.traffic_signs import TRAFFIC_SIGNS_TEMPLATE as HTML_TEMPLATE

from duckiebot.camera_driver.godot_camera_driver import GodotCameraDriver, GodotCameraConfig
from duckiebot.wheel_driver.godot_wheels_driver import GodotWheelsDriver
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

# Behaviour layer — same stack the bot runs, so --sim exercises real behaviour.
sign_sm          = None
motion           = None
surround_sensor  = None
stop_line_det    = StopLineDetector()

_obstacle_queue    = queue.Queue(maxsize=1)
_surroundings      = Surroundings.clear()
_surroundings_lock = threading.Lock()
_stop_line_ahead   = False

_last_tick_t  = None
_prev_state   = DRIVING
_control_lock = threading.Lock()

keys_pressed     = {'up': False, 'down': False, 'left': False, 'right': False}
_keys_lock       = threading.Lock()
_keys_last_update = time.time()


def _clamp01(x):
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else float(x)


def _mask_sign_regions(frame_rgb, detections, pad=12):
    """Black out detected sign tags before lane detection so the tag's white
    cells/border aren't mistaken for the white lane line (which makes the bot
    veer toward / drive into the sign). Detections come from a background thread,
    so they lag the live frame; we pad each box generously and IN PROPORTION to
    the tag's size (a close tag is large AND sweeps across the frame fastest), so
    the real tag stays covered even though it has moved since it was detected."""
    if not detections:
        return frame_rgb
    out = frame_rgb.copy()
    h, w = out.shape[:2]
    for d in detections:
        x1, y1, x2, y2 = d.bbox
        px = max(pad, int(0.6 * (x2 - x1)))
        py = max(pad, int(0.6 * (y2 - y1)))
        out[max(0, y1 - py):min(h, y2 + py), max(0, x1 - px):min(w, x2 + px)] = 0
    return out


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
            if manual_mode and not wheels.is_game_over():
                wheels.set_wheels_speed(left, right)
        time.sleep(0.05)


def visualize(frame_rgb):
    """frame_rgb is RGB from the Godot camera."""
    global _last_tick_t, _prev_state, _stop_line_ahead

    bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
    if wheels is None:
        return bgr

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

    _stop_line_ahead = stop_line_det.detect(frame_rgb)

    with _detection_lock:
        detections = list(_last_detections)
        active     = _active_sign
    with _surroundings_lock:
        surr = _surroundings
    surr = _dc_replace(surr, stop_line_ahead=_stop_line_ahead)

    lane_dbg = None
    if manual_mode:
        pass
    elif lane_agent is not None and sign_sm is not None and motion is not None:
        with _control_lock:
            now = time.monotonic()
            dt  = (now - _last_tick_t) if _last_tick_t is not None else 0.05
            _last_tick_t = now
            dt = max(0.01, min(0.2, dt))

            lane_cmd = lane_agent.compute_commands(_mask_sign_regions(frame_rgb, detections))
            lane_dbg = lane_agent.last_debug_info

            if not running or wheels.is_game_over():
                wheels.set_wheels_speed(0.0, 0.0)
                motion.sync(0.0, 0.0)
            else:
                cmd = sign_sm.update(active, surr, dt)
                if sign_sm.state == DRIVING and _prev_state not in (DRIVING, APPROACHING_SIGN, YIELDING):
                    reset_lane_follower(lane_agent)
                _prev_state = sign_sm.state
                if cmd.kind == 'lane_follow':
                    left  = _clamp01(lane_cmd[0] * cmd.speed_scale)
                    right = _clamp01(lane_cmd[1] * cmd.speed_scale)
                    # Lane lost ("searching"): the follower commands 0,0 and the
                    # bot stalls. Creep straight forward instead so it pushes
                    # through gaps / blank intersections until it re-finds the lane.
                    if not (lane_dbg or {}).get('lane_detected', True):
                        left = right = _clamp01(sign_sm.cfg.lane_lost_creep)
                    motion.sync(left, right)
                else:
                    left, right = motion.step(cmd, lane_cmd, dt)
                wheels.set_wheels_speed(left, right)

    if lane_dbg is not None:
        draw_lane_overlay(bgr, lane_dbg)
    draw_stop_line(bgr, stop_line_det)
    if detections:
        draw_signs(bgr, detections)
    draw_active_banner(bgr, active)
    if sign_sm is not None:
        draw_behavior_state(bgr, sign_sm.state, sign_sm.note)
    return bgr


generate_frames = make_frame_generator(lambda: camera, visualize, quality=50)


def _reset_behavior():
    global _prev_state, _last_tick_t, _surroundings
    if sign_sm is not None:
        sign_sm.reset()
    if motion is not None:
        motion.reset()
    if surround_sensor is not None:
        surround_sensor.reset()
    reset_lane_follower(lane_agent)
    with _surroundings_lock:
        _surroundings = Surroundings.clear()
    _prev_state  = DRIVING
    _last_tick_t = None


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, config=sign_agent, hostname=socket.gethostname(), virtual=True)

@app.route('/video')
def video():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

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

@app.route('/reset', methods=['POST'])
def reset():
    global _last_detections, _active_sign, running
    with _control_lock:
        if wheels:
            wheels.reset_game()
        _reset_behavior()
        running = True
    with _detection_lock:
        _last_detections = []
        _active_sign     = None
    return jsonify({'status': 'reset', 'running': running})

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

@app.route('/set_speed', methods=['POST'])
def set_speed():
    value = request.json.get('value') if request.json else None
    if lane_agent is not None and value is not None:
        v = max(0.0, min(1.0, float(value)))
        lane_agent.base_speed = v
        lane_agent.curve_speed = v
    return jsonify({'base_speed': getattr(lane_agent, 'base_speed', None)})

@app.route('/set_gains', methods=['POST'])
def set_gains():
    """Live lane-follower gain adjuster: P (steering / lateral) and D (damping).
    Takes effect on the next frame; not persisted to the config file."""
    data = request.json or {}
    if lane_agent is not None:
        if data.get('p_gain') is not None:
            lane_agent.p_gain = max(0.0, float(data['p_gain']))
        if data.get('d_gain') is not None:
            lane_agent.d_gain = max(0.0, float(data['d_gain']))
    return jsonify({'p_gain': getattr(lane_agent, 'p_gain', None),
                    'd_gain': getattr(lane_agent, 'd_gain', None)})

@app.route('/keys', methods=['POST'])
def update_keys():
    global _keys_last_update
    data = request.json or {}
    with _keys_lock:
        for k in keys_pressed:
            keys_pressed[k] = bool(data.get(k, False))
    _keys_last_update = time.time()
    return jsonify({'status': 'ok'})

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
        'game_over':      wheels.is_game_over() if wheels else False,
        'detector_ready': sign_agent is not None and sign_agent.load_error is None,
        'load_error':     sign_agent.load_error if sign_agent else None,
        'family':         sign_agent.family if sign_agent else None,
        'base_speed':     getattr(lane_agent, 'base_speed', None) if lane_agent else None,
        'p_gain':         getattr(lane_agent, 'p_gain', None) if lane_agent else None,
        'd_gain':         getattr(lane_agent, 'd_gain', None) if lane_agent else None,
        'active_sign':    active,
        'behavior_state': sign_sm.state if sign_sm else None,
        'behavior_note':  sign_sm.note if sign_sm else None,
        'chosen_turn':    sign_sm.chosen_turn if sign_sm else None,
        'obstacle_ahead': surr.obstacle_ahead,
        'robot_on_right': surr.robot_on_right,
        'stop_line':      _stop_line_ahead,
        'sensor_ready':   surround_sensor is not None and surround_sensor.model_loaded,
        'detections': [
            {'tag_id': d.tag_id, 'sign_type': d.sign_type, 'distance_m': d.distance_m,
             'pixel_size': d.pixel_size, 'turns': d.turns, 'offset_norm': d.offset_norm}
            for d in dets
        ],
    })


def main():
    global lane_agent, sign_agent, camera, wheels, sign_sm, motion, surround_sensor

    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--port',       type=int, default=5000)
    ap.add_argument('--frame-port', type=int, default=5001)
    ap.add_argument('--wheel-port', type=int, default=5002)
    ap.add_argument('--godot-host', type=str, default='localhost')
    args = ap.parse_args()

    suppress_http_logs()
    print('=' * 60)
    print('TRAFFIC SIGNS (SIM) — LANE FOLLOW + SIGN BEHAVIOUR STATE MACHINE')
    print('=' * 60)

    beh_cfg = BehaviorConfig.from_yaml()
    sign_sm = TrafficSignStateMachine(beh_cfg)
    motion  = MotionController(beh_cfg)

    print('\n[1/4] Creating lane + sign agents...')
    lane_agent = LaneServoingAgent()
    print(f'  lane speed={lane_agent.base_speed}')
    sign_agent = TrafficSignAgent()
    print(f'  sign backend={sign_agent.backend_name or sign_agent.load_error}')

    if SurroundingsSensor is not None:
        try:
            surround_sensor = SurroundingsSensor()
            print('  obstacle/right-of-way sensor:',
                  'ready' if surround_sensor.model_loaded else 'model unavailable (disabled)')
        except Exception as e:
            print(f'  obstacle sensor init failed: {e}')

    print('\n[2/4] Initializing wheels...')
    wheels = GodotWheelsDriver(
        WheelPWMConfiguration(pwm_min=0), WheelPWMConfiguration(pwm_min=0),
        godot_host=args.godot_host, godot_port=args.wheel_port,
    )

    print('\n[3/4] Initializing camera...')
    camera = GodotCameraDriver(godot_config=GodotCameraConfig(host='0.0.0.0', port=args.frame_port))
    camera.start()

    print('\n[4/4] Starting threads...')
    threading.Thread(target=detection_loop,      daemon=True).start()
    threading.Thread(target=obstacle_loop,       daemon=True).start()
    threading.Thread(target=manual_control_loop, daemon=True).start()

    web_port = find_available_port(args.port)
    print(f'\nWeb Interface: http://localhost:{web_port}')
    print('=' * 60 + '\n')

    try:
        app.run(host='127.0.0.1', port=web_port, debug=False, threaded=True)
    except KeyboardInterrupt:
        print('\nShutting down...')
    finally:
        shutdown_cleanup(wheels, camera, stop_event)


if __name__ == '__main__':
    sys.exit(main())
