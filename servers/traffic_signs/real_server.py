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

from tasks.visual_lane_servoing.packages.agent import LaneServoingAgent
from tasks.traffic_signs.packages.agent import TrafficSignAgent
from tasks.traffic_signs.packages import detection_activity as student
from servers.traffic_signs.visualization import draw_signs, draw_active_banner, draw_status_overlay
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


def visualize(frame_bgr):
    if wheels is None:
        return draw_status_overlay(frame_bgr, 'Initializing...')

    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

    if sign_agent is not None:
        try:
            _frame_queue.put_nowait(frame_rgb)
        except queue.Full:
            pass

    with _detection_lock:
        detections = list(_last_detections)
        active     = _active_sign

    if manual_mode:
        pass
    elif lane_agent is not None:
        pwm_left, pwm_right = lane_agent.compute_commands(frame_rgb)
        target = 0.0 if not running else 1.0
        wheels.set_wheels_speed(pwm_left * target, pwm_right * target)

    if detections:
        draw_signs(frame_bgr, detections)
    draw_active_banner(frame_bgr, active)
    return frame_bgr


generate_frames = make_frame_generator(lambda: camera, visualize, quality=50, rgb=False)


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, config=sign_agent, hostname=socket.gethostname(), virtual=False)

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

@app.route('/set_mode', methods=['POST'])
def set_mode():
    global manual_mode
    mode = request.json.get('mode', 'auto') if request.json else 'auto'
    manual_mode = (mode == 'manual')
    if wheels and not manual_mode:
        wheels.set_wheels_speed(0.0, 0.0)
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

@app.route('/status')
def status():
    with _detection_lock:
        dets = list(_last_detections)
        active = _active_sign
    return jsonify({
        'running':        running,
        'manual_mode':    manual_mode,
        'detector_ready': sign_agent is not None and sign_agent.load_error is None,
        'load_error':     sign_agent.load_error if sign_agent else None,
        'family':         sign_agent.family if sign_agent else None,
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
    ap.add_argument('--port', type=int, default=5000)
    args = ap.parse_args()

    suppress_http_logs()
    print('=' * 60)
    print('TRAFFIC SIGNS — LANE FOLLOW + APRILTAG SIGN RECOGNITION')
    print('=' * 60)

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
        global lane_agent, sign_agent
        lane_agent = LaneServoingAgent()
        print(f'[Init] Lane agent ready (speed={lane_agent.base_speed})')
        sign_agent = TrafficSignAgent()
        print(f'[Init] Sign agent ready (family={sign_agent.family})')

    threading.Thread(target=_init_wheels,        daemon=True).start()
    threading.Thread(target=_init_camera,        daemon=True).start()
    threading.Thread(target=_init_agents,        daemon=True).start()
    threading.Thread(target=detection_loop,      daemon=True).start()
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
