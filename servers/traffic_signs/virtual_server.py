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
from flask import Flask, Response, render_template_string, jsonify, request

from tasks.visual_lane_servoing.packages.agent import LaneServoingAgent
from tasks.traffic_signs.packages.agent import TrafficSignAgent
from tasks.traffic_signs.packages import detection_activity as student
from servers.traffic_signs.visualization import draw_signs, draw_active_banner, draw_lane_overlay
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

keys_pressed     = {'up': False, 'down': False, 'left': False, 'right': False}
_keys_lock       = threading.Lock()
_keys_last_update = time.time()


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

        if not wheels.is_game_over():
            wheels.set_wheels_speed(left, right)
        time.sleep(0.05)


def visualize(frame_rgb):
    bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
    if wheels is None:
        return bgr

    if sign_agent is not None:
        try:
            _frame_queue.put_nowait(frame_rgb)
        except queue.Full:
            pass

    with _detection_lock:
        detections = list(_last_detections)
        active     = _active_sign

    lane_dbg = None
    if manual_mode:
        pass
    elif lane_agent is not None:
        pwm_left, pwm_right = lane_agent.compute_commands(frame_rgb)
        lane_dbg = lane_agent.last_debug_info
        target = 0.0 if (not running or wheels.is_game_over()) else 1.0
        wheels.set_wheels_speed(pwm_left * target, pwm_right * target)

    if lane_dbg is not None:
        draw_lane_overlay(bgr, lane_dbg)
    if detections:
        draw_signs(bgr, detections)
    draw_active_banner(bgr, active)
    return bgr


generate_frames = make_frame_generator(lambda: camera, visualize, quality=50)


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, config=sign_agent, hostname=socket.gethostname(), virtual=True)

@app.route('/video')
def video():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/start', methods=['POST'])
def start():
    global running
    running = True
    return jsonify({'status': 'running'})

@app.route('/stop', methods=['POST'])
def stop():
    global running
    running = False
    if wheels:
        wheels.set_wheels_speed(0.0, 0.0)
    return jsonify({'status': 'stopped'})

@app.route('/reset', methods=['POST'])
def reset():
    global _last_detections, _active_sign, running
    if wheels:
        wheels.reset_game()
    running = True
    with _detection_lock:
        _last_detections = []
        _active_sign     = None
    return jsonify({'status': 'reset', 'running': running})

@app.route('/set_mode', methods=['POST'])
def set_mode():
    global manual_mode
    mode = request.json.get('mode', 'auto') if request.json else 'auto'
    manual_mode = (mode == 'manual')
    if wheels and not manual_mode:
        wheels.set_wheels_speed(0.0, 0.0)
    return jsonify({'mode': 'manual' if manual_mode else 'auto'})

@app.route('/set_speed', methods=['POST'])
def set_speed():
    value = request.json.get('value') if request.json else None
    if lane_agent is not None and value is not None:
        v = max(0.0, min(1.0, float(value)))
        lane_agent.base_speed = v
        lane_agent.curve_speed = v
    return jsonify({'base_speed': getattr(lane_agent, 'base_speed', None)})

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
    return jsonify({
        'running':        running,
        'manual_mode':    manual_mode,
        'game_over':      wheels.is_game_over() if wheels else False,
        'detector_ready': sign_agent is not None and sign_agent.load_error is None,
        'load_error':     sign_agent.load_error if sign_agent else None,
        'family':         sign_agent.family if sign_agent else None,
        'base_speed':     getattr(lane_agent, 'base_speed', None) if lane_agent else None,
        'active_sign':    active,
        'detections': [
            {'tag_id': d.tag_id, 'sign_type': d.sign_type, 'distance_m': d.distance_m,
             'turns': d.turns, 'offset_norm': d.offset_norm}
            for d in dets
        ],
    })


def main():
    global lane_agent, sign_agent, camera, wheels

    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--port',       type=int, default=5000)
    ap.add_argument('--frame-port', type=int, default=5001)
    ap.add_argument('--wheel-port', type=int, default=5002)
    ap.add_argument('--godot-host', type=str, default='localhost')
    args = ap.parse_args()

    suppress_http_logs()
    print('=' * 60)
    print('TRAFFIC SIGNS — LANE FOLLOW + APRILTAG SIGN RECOGNITION')
    print('=' * 60)
    print('NOTE: the Godot simulator has no AprilTags, so detections will be')
    print('      empty in sim. Use --webcam/--image in test_detector.py or')
    print('      run on hardware (real_server) to see signs.')

    print('\n[1/4] Creating lane agent...')
    lane_agent = LaneServoingAgent()
    print(f'  speed={lane_agent.base_speed}')

    print('\n[2/4] Creating sign agent...')
    sign_agent = TrafficSignAgent()

    print('\n[3/4] Initializing wheels...')
    wheels = GodotWheelsDriver(
        WheelPWMConfiguration(pwm_min=0), WheelPWMConfiguration(pwm_min=0),
        godot_host=args.godot_host, godot_port=args.wheel_port,
    )

    print('\n[4/4] Initializing camera...')
    camera = GodotCameraDriver(godot_config=GodotCameraConfig(host='0.0.0.0', port=args.frame_port))
    camera.start()

    threading.Thread(target=detection_loop,      daemon=True).start()
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
