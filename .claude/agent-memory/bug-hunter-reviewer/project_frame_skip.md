---
name: project-frame-skip
description: detect() returns None on intentionally skipped frames; default skip is 1 (every other frame)
metadata:
  type: project
---

ObjectDetectionAgent.detect() and TrafficSignAgent.detect() return None on
intentionally skipped frames (frame-skip). object_detection's
integration_activity.NUMBER_FRAMES_SKIPPED() default is **1** (process every
other frame); traffic_signs detection_activity default may differ (INTERFACE.md
says 0).

**Why:** A None result must be treated as "keep previous reading", NOT "all
clear". Reporting clear on a skipped frame causes spurious resume/flap.
Counter-based hysteresis (e.g. sensors._update_robot_on_right) counts only
NON-skipped frames because update() returns self._last early on None — so
frames_to_set/clear are in units of processed frames, not wall-clock frames.

**How to apply:** Any consumer of detect() must branch on `is None` before
treating an empty list as "nothing seen". Empty list [] == processed, saw
nothing; None == skipped.
