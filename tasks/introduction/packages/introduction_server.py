"""Hardware entry point for the `introduction` (manual keyboard drive) task — runs ON THE BOT.

The Duckiebot dashboard launches each task from a script shipped *inside* the
deployed package at ``tasks/<task>/packages/<task>_server.py`` (``launch.py --run``
packages ``tasks/<task>/packages`` + ``config/``). The simulation twin lives in
``servers/introduction/virtual_server.py``; this is its real-hardware version:
same ``/keys`` / ``/wheels`` / ``/speeds`` / ``/video`` API, but driving the real
``DaguWheelsDriver`` + ``CameraDriver``.

Two safety properties for remote / unattended driving:
  * **Speed cap** — ``manual_drive.get_motor_speeds`` returns up to 1.0 (full
    tilt), which is unsafe to drive blind. Key-driven speeds are scaled by
    ``speed_scale`` (default 0.35, live-tunable via ``/set_speed``); direct
    ``/wheels`` commands are hard-clamped to ``DIRECT_MAX``.
  * **Command watchdog** — if no ``/keys`` or ``/wheels`` command arrives within
    ``CMD_WATCHDOG_S`` the wheels are driven to zero, so a dropped connection or a
    crashed client stops the bot instead of letting it run away.

It depends only on base infra every Duckiebot repo already has (``duckiebot``
drivers, ``servers.common``, ``launcher.ports``) plus this task's own
``manual_drive`` package; the richer UI from ``servers/templates`` is used when
present and otherwise falls back to a self-contained page defined below.
"""

import sys
import os
import signal
import threading
import time
import socket

script_dir   = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.normpath(os.path.join(script_dir, '..', '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import cv2
import numpy as np
from flask import Flask, Response, render_template_string, request, jsonify

from tasks.introduction.packages import manual_drive
from duckiebot.camera_driver import CameraDriver
from duckiebot.wheel_driver import DaguWheelsDriver
from duckiebot.wheel_driver.wheels_driver_abs import WheelPWMConfiguration
from launcher.ports import find_available_port
from servers.common import make_frame_generator, shutdown_cleanup, suppress_http_logs

# --- Safety tunables --------------------------------------------------------
MANUAL_SPEED_SCALE = 0.35   # key-driven speed = get_motor_speeds(...) * this
DIRECT_MAX         = 0.6    # hard clamp on direct /wheels magnitude
CMD_WATCHDOG_S     = 0.5    # no command in this long -> wheels to zero

# Full UI when the repo's servers/ is on the bot; else a compact inline page.
try:
    from servers.templates.introduction import INTRODUCTION_TEMPLATE as HTML_TEMPLATE
except Exception:
    HTML_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Introduction — Manual Drive</title>
<style>
body{background:#13161a;color:#e6edf3;font-family:sans-serif;margin:0;padding:16px}
img{max-width:100%;border-radius:6px}
#bar{margin:10px 0;font-size:14px}.k{color:#8b949e}
.kbd{display:inline-block;min-width:28px;padding:6px 8px;margin:2px;text-align:center;border:1px solid #30363d;border-radius:5px;background:#21262d}
.kbd.on{background:#2ea043;color:#fff;border-color:#2ea043}
</style></head><body>
<h2>Introduction — Manual Drive ({{ hostname }})</h2>
<img src="{{ url_for('video') }}">
<div id="bar">
  <span class="k">L:</span> <b id="vl">0.00</b>
  <span class="k">R:</span> <b id="vr">0.00</b>
  <span class="k" style="margin-left:14px">speed:</span>
  <input id="sp" type="range" min="0.1" max="1.0" step="0.05" value="0.35" oninput="onScale(this.value)">
  <b id="spv">0.35</b>
</div>
<div id="bar">
  <span class="kbd" id="k-up">&#9650;</span>
  <span class="kbd" id="k-left">&#9664;</span>
  <span class="kbd" id="k-down">&#9660;</span>
  <span class="kbd" id="k-right">&#9654;</span>
  <span class="k">arrow keys / WASD</span>
</div>
<script>
const ks={up:false,down:false,left:false,right:false};
const map={ArrowUp:'up',w:'up',W:'up',ArrowDown:'down',s:'down',S:'down',
           ArrowLeft:'left',a:'left',A:'left',ArrowRight:'right',d:'right',D:'right'};
function draw(){for(const k in ks){const e=document.getElementById('k-'+k);if(e)e.classList.toggle('on',ks[k]);}}
function send(){fetch('/keys',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(ks)}).catch(()=>{});}
document.addEventListener('keydown',e=>{const d=map[e.key];if(d&&!ks[d]){e.preventDefault();ks[d]=true;draw();send();}});
document.addEventListener('keyup',e=>{const d=map[e.key];if(d&&ks[d]){e.preventDefault();ks[d]=false;draw();send();}});
window.addEventListener('blur',()=>{for(const k in ks)ks[k]=false;draw();send();});
setInterval(()=>{if(Object.values(ks).some(Boolean))send();},150);
function onScale(v){document.getElementById('spv').textContent=parseFloat(v).toFixed(2);
 fetch('/set_speed',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scale:parseFloat(v)})});}
async function poll(){try{const d=await(await fetch('/speeds')).json();
 document.getElementById('vl').textContent=Number(d.left).toFixed(2);
 document.getElementById('vr').textContent=Number(d.right).toFixed(2);}catch(e){}}
setInterval(poll,200);poll();
</script></body></html>"""


app    = Flask(__name__)
camera = None
wheels = None
stop_event = threading.Event()

_keys_lock       = threading.Lock()
keys_pressed     = {'up': False, 'down': False, 'left': False, 'right': False}
_direct          = (0.0, 0.0)          # last /wheels command
_cmd_mode        = 'keys'              # 'keys' | 'direct'
_last_cmd_t      = 0.0                 # monotonic time of the last command
speed_scale      = MANUAL_SPEED_SCALE
current_speeds   = {'left': 0.0, 'right': 0.0}
_wheels_lock     = threading.Lock()


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def control_loop():
    """Sole writer to the wheels. Applies the freshest command (keys or direct),
    or zero if nothing has arrived within the watchdog window."""
    global current_speeds
    print('[ControlLoop] Starting (speed_scale=%.2f, watchdog=%.1fs)' % (speed_scale, CMD_WATCHDOG_S))
    while not stop_event.is_set():
        try:
            stale = (time.monotonic() - _last_cmd_t) > CMD_WATCHDOG_S
            if stale:
                left, right = 0.0, 0.0
            elif _cmd_mode == 'direct':
                left, right = _direct
            else:
                with _keys_lock:
                    kc = keys_pressed.copy()
                try:
                    left, right = manual_drive.get_motor_speeds(kc)
                except Exception as e:
                    print(f'[ControlLoop] manual_drive error: {e}')
                    left, right = 0.0, 0.0
                left  *= speed_scale
                right *= speed_scale

            left  = _clamp(left,  -1.0, 1.0)
            right = _clamp(right, -1.0, 1.0)
            current_speeds['left'], current_speeds['right'] = left, right
            if wheels is not None:
                with _wheels_lock:
                    wheels.set_wheels_speed(left, right)
            time.sleep(0.05)   # 20 Hz
        except Exception as e:
            print(f'[ControlLoop] error: {e}')
            time.sleep(0.1)
    print('[ControlLoop] Stopped')


def visualize(frame_bgr):
    """Overlay the live wheel speeds + a key indicator on the camera feed
    (frame is BGR — make_frame_generator is created with rgb=False)."""
    if frame_bgr is None:
        ph = np.zeros((240, 640, 3), dtype=np.uint8)
        cv2.putText(ph, 'Waiting for camera...', (160, 120),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (100, 100, 100), 2)
        return ph
    out = frame_bgr
    h, w = out.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(out, f"L:{current_speeds['left']:+.2f}  R:{current_speeds['right']:+.2f}",
                (10, h - 12), font, 0.6, (0, 255, 0), 2, cv2.LINE_AA)
    with _keys_lock:
        kc = keys_pressed.copy()
    ks, gap = 26, 4
    bx, by = w - 3 * (ks + gap) - 10, h - 2 * (ks + gap) - 14
    pos = {'up': (bx + ks + gap, by), 'left': (bx, by + ks + gap),
           'down': (bx + ks + gap, by + ks + gap), 'right': (bx + 2 * (ks + gap), by + ks + gap)}
    lbl = {'up': '^', 'down': 'v', 'left': '<', 'right': '>'}
    for k, (kx, ky) in pos.items():
        c = (0, 200, 0) if kc.get(k) else (60, 60, 60)
        cv2.rectangle(out, (kx, ky), (kx + ks, ky + ks), c, -1)
        cv2.putText(out, lbl[k], (kx + 7, ky + 19), font, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
    return out


generate_frames = make_frame_generator(lambda: camera, visualize, quality=60, rgb=False)


@app.route('/')
def index():
    try:
        return render_template_string(HTML_TEMPLATE, hostname=socket.gethostname(),
                                      title='Introduction — Manual Drive',
                                      subtitle='Drive with arrow keys / WASD')
    except Exception:
        return render_template_string(HTML_TEMPLATE, hostname=socket.gethostname())

@app.route('/video')
def video():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/keys', methods=['POST'])
def update_keys():
    global keys_pressed, _cmd_mode, _last_cmd_t
    data = request.json or {}
    with _keys_lock:
        keys_pressed = {k: bool(data.get(k, False)) for k in ('up', 'down', 'left', 'right')}
    _cmd_mode   = 'keys'
    _last_cmd_t = time.monotonic()
    return jsonify({'status': 'ok', 'left': current_speeds['left'], 'right': current_speeds['right']})


@app.route('/wheels', methods=['POST'])
def set_wheels():
    """Directly command wheel speeds (hard-clamped to +/- DIRECT_MAX for safety).
    Watchdog still applies, so the command must be refreshed to keep moving."""
    global _direct, _cmd_mode, _last_cmd_t
    data = request.json or {}
    left  = _clamp(float(data.get('left', 0.0)),  -DIRECT_MAX, DIRECT_MAX)
    right = _clamp(float(data.get('right', 0.0)), -DIRECT_MAX, DIRECT_MAX)
    _direct     = (left, right)
    _cmd_mode   = 'direct'
    _last_cmd_t = time.monotonic()
    return jsonify({'status': 'ok', 'left': left, 'right': right})


@app.route('/stop', methods=['POST'])
def stop():
    """Immediate stop: clear keys + direct so the watchdog/loop holds zero."""
    global keys_pressed, _direct, _last_cmd_t
    with _keys_lock:
        keys_pressed = {'up': False, 'down': False, 'left': False, 'right': False}
    _direct = (0.0, 0.0)
    _last_cmd_t = 0.0
    if wheels is not None:
        with _wheels_lock:
            wheels.set_wheels_speed(0.0, 0.0)
    return jsonify({'status': 'stopped'})


@app.route('/set_speed', methods=['POST'])
def set_speed():
    """Live safety-cap adjuster for key-driven speed (0..1)."""
    global speed_scale
    data = request.json or {}
    v = data.get('scale', data.get('value'))
    if v is not None:
        speed_scale = _clamp(float(v), 0.0, 1.0)
    return jsonify({'scale': speed_scale})


@app.route('/speeds')
def get_speeds():
    return jsonify(current_speeds)


@app.route('/status')
def status():
    return jsonify({
        'running':      camera is not None and wheels is not None,
        'manual_mode':  True,
        'behavior_state': 'MANUAL',
        'speed_scale':  speed_scale,
        'left':         current_speeds['left'],
        'right':        current_speeds['right'],
        'keys':         keys_pressed,
    })


# LED endpoints stubbed (no-op) for API compatibility with the full UI; manual
# driving doesn't need them, and this keeps the page from erroring on the bot.
@app.route('/leds', methods=['POST'])
def _led():        return jsonify({'status': 'ok'})
@app.route('/leds/all', methods=['POST'])
def _led_all():    return jsonify({'status': 'ok'})
@app.route('/leds/off', methods=['POST'])
def _led_off():    return jsonify({'status': 'ok'})
@app.route('/leds/state')
def _led_state():  return jsonify({})


def main():
    global camera, wheels

    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=5000)
    args = ap.parse_args()

    suppress_http_logs()
    print('=' * 60)
    print('INTRODUCTION — MANUAL KEYBOARD DRIVE (REAL HARDWARE)')
    print(f'  safety: speed_scale={MANUAL_SPEED_SCALE}  direct_max={DIRECT_MAX}  watchdog={CMD_WATCHDOG_S}s')
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

    threading.Thread(target=_init_wheels,  daemon=True).start()
    threading.Thread(target=_init_camera,  daemon=True).start()
    threading.Thread(target=control_loop,  daemon=True).start()

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
