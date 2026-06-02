---
name: project-traffic-signs-coupling
description: traffic_signs behavior layer (Person B) imports object_detection + visual_lane_servoing packages at module load
metadata:
  type: project
---

`tasks/traffic_signs/packages/sensors.py` imports
`tasks.object_detection.packages.stop_activity.should_stop` at MODULE LOAD
(top-level), and lazily imports `ObjectDetectionAgent` in `SurroundingsSensor.__init__`.
`servers/traffic_signs/real_server.py` imports `LaneServoingAgent` from
`tasks.visual_lane_servoing.packages.agent`.

**Why:** Person B reuses object_detection for obstacle/right-of-way and the lane
agent for steering, instead of reimplementing them.

**How to apply:** The graceful-degradation story ("best.onnx missing -> sensor
inert") only covers the MODEL file. It does NOT cover the object_detection
*Python package* being absent: the top-level `from tasks.object_detection...`
import in sensors.py will raise ImportError before any graceful fallback runs.
should_stop() also keeps module-GLOBAL hysteresis state (_state/_clear_streak) —
fine while traffic_signs is the only caller in-process. See
[[project-deploy-packaging]].
