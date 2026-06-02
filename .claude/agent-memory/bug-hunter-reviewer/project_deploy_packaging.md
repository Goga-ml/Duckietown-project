---
name: project-deploy-packaging
description: launch.py --run only packages the named task's packages dir + config + that task's models; cross-task deps are NOT shipped
metadata:
  type: project
---

`launch.py` `package_task(task_name)` tars ONLY:
- `tasks/<task>/packages/`
- `config/`
- `tasks/<task>/models/` (if it exists)

**Why:** This is the deploy contract for `--run`. Anything a task imports from a
*different* task's `packages/` (or another task's `models/`) is NOT in the tarball
and will be missing on the bot unless that other task was deployed previously.

**How to apply:** When reviewing a task that imports `tasks.<other_task>.packages.*`
or another task's model file, flag it as a deploy-time ImportError / missing-file
risk on a clean bot. traffic_signs is the live example: it imports from
object_detection and visual_lane_servoing. See [[project-traffic-signs-coupling]].
best.onnx lives under tasks/object_detection/models/ and is git-ignored, so it is
also not shipped by `traffic_signs --run`.
