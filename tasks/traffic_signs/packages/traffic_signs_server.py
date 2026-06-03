"""Hardware entry point for the traffic_signs task — runs ON THE BOT.

The Duckiebot dashboard launches each task from a script shipped *inside* the
deployed package, at ``tasks/<task>/packages/<task>_server.py``. ``launch.py
--run`` only packages ``tasks/<task>/packages/`` + ``config/``, so the server
that runs on the bot must live here, not under ``servers/``.

This file is therefore deliberately **self-contained**: it depends only on
  * base infra that every Duckiebot repo already has — ``duckiebot`` drivers,
    ``servers.common``, ``servers.templates.base``, ``launcher.ports``, and the
    standard ``visual_lane_servoing`` / ``object_detection`` task packages, and
  * the traffic_signs package modules that ship alongside it (agent,
    detection_activity, state_machine, sensors, sign_lookup).

The richer overlay/UI from ``servers/traffic_signs`` is used automatically when
present (i.e. when this repo is checked out on the bot), and otherwise falls
back to compact inline versions defined below — so it runs either way.

For local development you normally use ``servers/traffic_signs/real_server.py``
(via the dashboard on your machine); this file mirrors its behaviour and is the
one that actually executes on the robot.
"""

import sys
import os
import signal
import threading
import time
import queue
import socket

# packages/ is three levels below the project root.
script_dir   = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.normpath(os.path.join(script_dir, '..', '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import cv2
import numpy as np
from flask import Flask, Response, render_template_string, jsonify, request

from tasks.visual_lane_servoing.packages.agent import LaneServoingAgent
from tasks.traffic_signs.packages.agent import TrafficSignAgent
from dataclasses import replace as _dc_replace
from tasks.traffic_signs.packages import detection_activity as student
from tasks.traffic_signs.packages.state_machine import (
    TrafficSignStateMachine, MotionController, BehaviorConfig,
    Surroundings, reset_lane_follower, DRIVING, APPROACHING_SIGN,
)
from tasks.traffic_signs.packages.stop_line import StopLineDetector
try:
    from tasks.traffic_signs.packages.sensors import SurroundingsSensor
except Exception as _sensor_import_err:   # object_detection package/model absent
    SurroundingsSensor = None
    print('[Init] Surroundings sensor unavailable '
          f'({_sensor_import_err}); obstacle-stop & right-of-way DISABLED, '
          'sign behaviours still active.')

from duckiebot.camera_driver import CameraDriver
from duckiebot.wheel_driver import DaguWheelsDriver
from duckiebot.wheel_driver.wheels_driver_abs import WheelPWMConfiguration
from launcher.ports import find_available_port
from servers.common import make_frame_generator, shutdown_cleanup, suppress_http_logs


# ---------------------------------------------------------------------------
# Visualization + HTML template: use the full versions from servers/ when this
# repo is on the bot, else fall back to compact self-contained implementations.
# ---------------------------------------------------------------------------
try:
    from servers.traffic_signs.visualization import (
        draw_signs, draw_active_banner, draw_status_overlay, draw_behavior_state,
        draw_lane_overlay, draw_stop_line,
    )
except Exception:
    _STATE_COLORS = {
        "DRIVING": (60, 200, 60), "APPROACHING_SIGN": (0, 200, 255),
        "STOPPED": (60, 60, 220), "WAITING_FOR_RIGHT_OF_WAY": (0, 140, 255),
        "TURNING": (220, 170, 50), "OBSTACLE_STOP": (60, 60, 220),
    }

    def draw_signs(img, detections):
        for d in detections:
            color = (60, 200, 60) if d.sign_type else (160, 160, 160)
            cv2.polylines(img, [d.corners.astype(np.int32)], True, color, 2)
            label = f"{d.sign_type or ('id ' + str(d.tag_id))} {d.distance_m:.2f}m"
            x1, y1 = d.bbox[0], d.bbox[1]
            cv2.putText(img, label, (x1, max(12, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
        return img

    def draw_active_banner(img, active):
        if not active:
            return img
        turns = active.get("turns")
        txt = (f"ACTIVE: {active['sign_type']} {active['distance_m']}m"
               + (f" turns={','.join(turns)}" if turns else "")
               + ("  [AT SIGN]" if active.get("at_sign") else ""))
        cv2.putText(img, txt, (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 220, 255), 2, cv2.LINE_AA)
        return img

    def draw_behavior_state(img, state, note="", y=44):
        txt = f"STATE: {state}" + (f"  |  {note}" if note else "")
        cv2.putText(img, txt, (10, y + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    _STATE_COLORS.get(state, (200, 200, 200)), 1, cv2.LINE_AA)
        return img

    def draw_status_overlay(img, message):
        out = img.copy()
        cv2.putText(out, message, (16, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 200, 255), 1, cv2.LINE_AA)
        return out

    def draw_stop_line(img, detector):
        if detector is None:
            return img
        h, w = img.shape[:2]
        x0, x1 = int(w * detector.roi_x0), int(w * detector.roi_x1)
        y0 = int(h * detector.roi_top)
        hit = detector.last_ratio >= detector.min_area_ratio
        color = (0, 0, 255) if hit else (120, 120, 120)
        cv2.rectangle(img, (x0, y0), (x1, h - 1), color, 2)
        cv2.putText(img, f"stop-line {detector.last_ratio:.2f}" + (" REACHED" if hit else ""),
                    (x0 + 4, max(12, y0 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2, cv2.LINE_AA)
        return img

    def draw_lane_overlay(img, lane_dbg, tol=5):
        if not lane_dbg:
            return img
        h, w = img.shape[:2]
        ym = lane_dbg.get('yellow_mask'); wm = lane_dbg.get('white_mask')
        cv2.line(img, (w // 2, int(h * 0.45)), (w // 2, h), (90, 90, 90), 1)
        for y in (lane_dbg.get('slice_ys') or []):
            if not (0 <= y < h):
                continue
            for mask, color in ((ym, (0, 255, 255)), (wm, (255, 255, 255))):
                if mask is None or mask.shape[:2] != (h, w):
                    continue
                xs = np.where(mask[max(0, y - tol):y + tol, :] > 0)[1]
                if len(xs):
                    mx = int(xs.mean())
                    cv2.circle(img, (mx, y), 6, color, -1)
                    cv2.circle(img, (mx, y), 6, (0, 0, 0), 1)
        detected = bool(lane_dbg.get('lane_detected'))
        cv2.putText(img, "LANE: tracking" if detected else "LANE: searching",
                    (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (60, 200, 60) if detected else (0, 170, 255), 2, cv2.LINE_AA)
        return img

try:
    from servers.templates.traffic_signs import TRAFFIC_SIGNS_TEMPLATE as HTML_TEMPLATE
except Exception:
    HTML_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Traffic Signs</title>
<style>
body{background:#13161a;color:#e6edf3;font-family:sans-serif;margin:0;padding:16px}
img{max-width:100%;border-radius:6px}
button{padding:10px 16px;margin:4px;border:0;border-radius:5px;font-size:14px;cursor:pointer}
#go{background:#2ea043;color:#fff}#halt{background:#d29922;color:#000}
#bar{margin:10px 0;font-size:14px}.k{color:#8b949e}
</style></head><body>
<h2>Traffic Signs — {{ hostname }}</h2>
<img src="{{ url_for('video') }}">
<div id="bar">
  <button id="go" onclick="post('/start')">Start</button>
  <button id="halt" onclick="post('/stop')">Stop</button>
  <span class="k">state:</span> <b id="st">-</b>
  <span class="k">sign:</span> <b id="sg">-</b>
  <span class="k">detector:</span> <b id="dr">-</b>
</div>
<div id="bar">
  <span class="k">speed:</span>
  <input id="sp" type="range" min="0.05" max="0.5" step="0.01" value="0.2" style="vertical-align:middle;width:240px" oninput="onSpeed(this.value)">
  <b id="spv">0.20</b>
</div>
<script>
function post(u){fetch(u,{method:'POST'});}
function postJSON(u,d){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});}
let spDirty=false;
function onSpeed(v){document.getElementById('spv').textContent=parseFloat(v).toFixed(2);spDirty=true;
 postJSON('/set_speed',{value:parseFloat(v)}).then(()=>{spDirty=false;});}
async function poll(){try{let d=await(await fetch('/status')).json();
 document.getElementById('st').textContent=d.behavior_state||'-';
 let a=d.active_sign; document.getElementById('sg').textContent=a?(a.sign_type+' '+a.distance_m+'m'):'none';
 document.getElementById('dr').textContent=d.detector_ready?('ready ('+(d.family||'')+')'):(d.load_error||'…');
 if(!spDirty && d.base_speed!=null){document.getElementById('sp').value=d.base_speed;document.getElementById('spv').textContent=Number(d.base_speed).toFixed(2);}
}catch(e){}}
setInterval(poll,300);poll();
</script></body></html>"""


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

sign_sm          = None
motion           = None
surround_sensor  = None
stop_line_det    = StopLineDetector()   # red stop-line detector (no heavy deps)

_obstacle_queue    = queue.Queue(maxsize=1)
_surroundings      = Surroundings.clear()
_surroundings_lock = threading.Lock()
_stop_line_ahead   = False              # last red-line reading (for /status)

_last_tick_t  = None
_prev_state   = DRIVING
_control_lock = threading.Lock()

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
            if manual_mode:
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
        pass
    elif lane_agent is not None and sign_sm is not None and motion is not None:
        with _control_lock:
            now = time.monotonic()
            dt  = (now - _last_tick_t) if _last_tick_t is not None else 0.05
            _last_tick_t = now
            dt = max(0.01, min(0.2, dt))

            # Always compute lane following (cheap) so the overlay shows what the
            # bot sees even before /start; only the wheels are gated on `running`.
            lane_cmd = lane_agent.compute_commands(frame_rgb)
            lane_dbg = lane_agent.last_debug_info

            if not running:
                wheels.set_wheels_speed(0.0, 0.0)
            else:
                cmd = sign_sm.update(active, surr, dt)
                if sign_sm.state == DRIVING and _prev_state not in (DRIVING, APPROACHING_SIGN):
                    reset_lane_follower(lane_agent)
                _prev_state = sign_sm.state
                if cmd.kind == 'lane_follow':
                    # Plain lane following: drive exactly like the
                    # visual_lane_servoing task — apply the lane agent's wheel
                    # commands directly (no slew limiting) so steering stays
                    # crisp. Keep the motion controller synced so a later
                    # halt/turn still ramps down smoothly from the real speed.
                    left  = float(np.clip(lane_cmd[0] * cmd.speed_scale, 0.0, 1.0))
                    right = float(np.clip(lane_cmd[1] * cmd.speed_scale, 0.0, 1.0))
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
