# CLAUDE.md

Guidance for working in this repository.

## What this is

A Duckietown project: autonomous Duckiebot behaviours that run **both in a Godot
simulator and on real hardware** from the same Python code. Each "task" is a
self-contained behaviour (lane following, object detection, traffic signs, …)
with a perception/control agent, a web-based server, and a Godot scene.

## Architecture

```
launch.py              # entrypoint: --sim (Godot) | --run (deploy to bot) | --stop
launcher/config.py     # GODOT_SCENES: task name -> Godot scene; ports
config/                # per-task YAML config (one file per task)
duckiebot/             # hardware + sim drivers (camera, wheels, LEDs, encoders)
servers/
  common.py            # make_frame_generator (MJPEG), shutdown, log filters
  templates/           # shared HTML (base.py) + per-task web UI
  <task>/
    virtual_server.py  # Flask app wired to the Godot sim drivers
    real_server.py     # Flask app wired to the hardware drivers
    visualization.py   # OpenCV overlay drawing
tasks/
  <task>/
    packages/
      agent.py             # the perception/control logic (imported by servers)
      <activity>.py        # student-editable tuning/logic (imported by agent)
    notebooks/             # teaching notebooks (not used at runtime)
GodotSimulation/ducky-bot/ # Godot 4.6 project (scenes, assets)
```

### The task pattern (how every task is shaped)

- **`tasks/<task>/packages/agent.py`** holds an `Agent` class. The server calls
  it once per frame. It loads its tuning from `config/<task>_config.yaml`.
- **Student-editable activity files** (e.g. `integration_activity.py`,
  `stop_activity.py`, `detection_activity.py`) hold the logic/parameters meant to
  be tuned. `agent.py` imports them as `student` and calls their functions. Keep
  this split: stable plumbing in `agent.py`, tunable logic in the activity file.
- **`servers/<task>/{virtual,real}_server.py`** are near-mirror Flask apps. The
  only real difference is the drivers:
  - virtual: `GodotCameraDriver` / `GodotWheelsDriver`
  - real: `CameraDriver` / `DaguWheelsDriver`
  They share a `visualize(frame)` callback fed to `make_frame_generator`, plus
  `/start`, `/stop`, `/status`, `/keys`, `/set_mode` routes.
- **`launcher/config.py`** must map the task name to a Godot scene in
  `GODOT_SCENES` for `--sim` to work.

### Frame colour convention (easy to get wrong)

- `make_frame_generator(..., rgb=True)` calls `camera.read_rgb()` and passes an
  **RGB** frame to `visualize`. Used by virtual servers.
- `rgb=False` calls `camera.read()` (**BGR**) — used by real servers.
- Agents generally want RGB; convert explicitly (`cv2.cvtColor`) at the boundary.
  OpenCV drawing/encoding expects BGR.

## Running

```bash
# Simulation (downloads Godot 4.6 on first run, launches scene + server)
python launch.py --sim --task <task>
python launch.py --sim --task <task> --debug      # show Godot console

# Hardware: package tasks/<task>/packages + config + models, deploy, start
python launch.py --run --bot <hostname> --task <task>
python launch.py --run --host <ip> --task <task>
python launch.py --stop --bot <hostname>
```

Web UI prints its URL on startup (default port 5000). `--sim` reuses a Godot
scene per `GODOT_SCENES`; not every task has its own scene.

## Environment

- Python deps in `requirements.txt` (Flask, OpenCV `<4.10`, NumPy `<2`, onnxruntime…).
- Local venv lives in `.venv/` (git-ignored). Use `./.venv/Scripts/python.exe` on Windows.
- `*.zip` and the object-detection `dataset/` & `models/` dirs are git-ignored.

## Tasks present

- **visual_lane_servoing** — HSV lane-mask + PD steering (`LaneServoingAgent`).
- **object_detection** — YOLOv5n ONNX detector (`ObjectDetectionAgent`) + a stop
  state machine (`stop_activity.py`); lane-follows and stops for obstacles.
- **traffic_signs** — AprilTag sign recognition (`TrafficSignAgent`, `cv2.aruco`).
  Perception layer (Person A). See `tasks/traffic_signs/INTERFACE.md` for the
  contract the behaviour/state-machine layer (Person B) builds on.
- braitenberg, introduction, modcon — earlier teaching tasks.

## Conventions

- Match the surrounding file's style: type hints, module-level tunables with
  explanatory comments, `print('[Component] ...')` logging.
- Don't add runtime dependencies casually — prefer what's already available
  (e.g. AprilTags use the built-in `cv2.aruco`, no new package).
- Validate perception/logic changes with a standalone script where possible
  (see `tasks/traffic_signs/packages/test_detector.py`) before needing the bot.
