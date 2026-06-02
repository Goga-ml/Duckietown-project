from typing import Tuple

# Path to the trained model weights (.onnx file).
# Relative paths resolve from the project root.
MODEL_PATH = "tasks/object_detection/models/best.onnx"

# The detection pipeline feeds an img_size x img_size (default 416) frame to
# the detector, so bbox pixel coordinates here live in that square.
_REF_SIZE = 416


def NUMBER_FRAMES_SKIPPED() -> int:
    # 1 = run inference every other frame. At simulator framerate this is
    # ~12-15 Hz of detections, which the should_stop hysteresis tolerates
    # without late-braking on obstacles. Raise to 2-3 only if the bot's CPU
    # can't keep up; lower to 0 if you see the bot react sluggishly.
    return 1


def filter_by_classes(pred_class: int) -> bool:
    # 0=duckie, 1=truck, 2=sign. Signs do not block motion in this exercise.
    return pred_class in (0, 1)


def filter_by_scores(score: float) -> bool:
    # A freshly trained YOLOv5n on ~280 simulator frames is noticeably less
    # confident than COCO weights. 0.35 keeps the bot reacting to partial
    # detections; raise once the model is more confident.
    return score >= 0.35


def filter_by_bboxes(bbox: Tuple[int, int, int, int]) -> bool:
    x1, y1, x2, y2 = bbox
    w, h = x2 - x1, y2 - y1
    if w < 12 or h < 12:
        return False
    # Anything sitting entirely above the horizon line is sky/background noise.
    if y2 < _REF_SIZE * 0.35:
        return False
    return True
