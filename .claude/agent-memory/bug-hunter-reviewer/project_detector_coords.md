---
name: project-detector-coords
description: object_detection detect() bbox coordinate space and how should_stop reuse must square the frame
metadata:
  type: project
---

ObjectDetectionAgent.detect(frame_rgb) returns bboxes scaled back to the INPUT
frame's (orig_w, orig_h) pixel space — NOT img_size space. (`_postprocess` /
`_xywh2xyxy` / `_postprocess_xyxy` multiply by orig_w/orig_h.)

**Why:** should_stop(detections, img_size) and traffic_signs sensors._in_right_zone
both expect SQUARE img_size pixel coords. The only way to get that from detect()
is to feed it an already-square frame (cv2.resize(frame, (size, size))) so
orig_w == orig_h == size. traffic_signs/packages/sensors.py does this correctly
in update(); object_detection real_server also pre-squares before queueing.

**How to apply:** When reviewing any reuse of detect()+should_stop, verify the
frame handed to detect() is square == img_size. If a raw 640x480 frame is passed
to detect() and the result fed to should_stop(., img_size), the coords are wrong.
Squashing 4:3 -> square distorts aspect but preserves cx as a fraction of width,
and the model is used the same way everywhere, so that squash is intentional.

filter_by_classes in object_detection/integration_activity.py returns only
classes 0 (duckie) and 1 (truck) — class 2 (sign) is filtered out, so detect()
never yields sign boxes downstream.
