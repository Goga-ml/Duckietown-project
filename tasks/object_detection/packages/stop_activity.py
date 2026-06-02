from typing import List, Tuple

Detection = Tuple[Tuple[int, int, int, int], float, int]

class_names = {0: 'duckie', 1: 'truck', 2: 'sign'}

# Two-state machine, persisted across should_stop() calls:
#   FOLLOWING -> OBSTACLE: a detection enters the (tight) trigger zone.
#   OBSTACLE  -> FOLLOWING: no detection in the (loose) hold zone for
#                          _FRAMES_TO_CLEAR consecutive frames.
# The asymmetry is hysteresis so a wobbling bbox at the edge doesn't flap us
# in and out of stopping.
_STATE_FOLLOWING = 'lane_following'
_STATE_OBSTACLE  = 'obstacle_present'

_state         = _STATE_FOLLOWING
_clear_streak  = 0

# Trigger zone (used while FOLLOWING): tight.
_ENTER_HALF_WIDTH = 0.45  # |cx - center| <= this * (img_size/2)
_ENTER_MIN_AREA   = 0.018 # bbox area / frame area (~2% -> roughly 1m in sim)
_ENTER_BOTTOM_MIN = 0.40  # bbox bottom must reach this fraction of frame height

# Hold zone (used while OBSTACLE): loose, so a flickering detection still keeps us stopped.
_EXIT_HALF_WIDTH  = 0.70
_EXIT_MIN_AREA    = 0.010
_EXIT_BOTTOM_MIN  = 0.25

_FRAMES_TO_CLEAR  = 5


def _in_zone(bbox, img_size, *, enter: bool) -> bool:
    x1, y1, x2, y2 = bbox
    cx     = (x1 + x2) * 0.5
    area   = (x2 - x1) * (y2 - y1)
    bottom = y2

    center  = img_size * 0.5
    img_area = img_size * img_size

    half_w_ratio = _ENTER_HALF_WIDTH if enter else _EXIT_HALF_WIDTH
    min_area     = _ENTER_MIN_AREA   if enter else _EXIT_MIN_AREA
    bottom_min   = (_ENTER_BOTTOM_MIN if enter else _EXIT_BOTTOM_MIN) * img_size

    if abs(cx - center) > half_w_ratio * center:
        return False
    if area / img_area < min_area:
        return False
    if bottom < bottom_min:
        return False
    return True


def should_stop(detections: List[Detection], img_size: int) -> Tuple[bool, str]:
    global _state, _clear_streak

    enter = (_state == _STATE_FOLLOWING)
    blocker = None
    for bbox, score, cls_id in detections:
        if _in_zone(bbox, img_size, enter=enter):
            blocker = (bbox, score, cls_id)
            break

    if _state == _STATE_FOLLOWING:
        if blocker is not None:
            _state = _STATE_OBSTACLE
            _clear_streak = 0
            name = class_names.get(blocker[2], str(blocker[2]))
            return True, f'{name} in lane'
        return False, ''

    # _STATE_OBSTACLE
    if blocker is not None:
        _clear_streak = 0
        name = class_names.get(blocker[2], str(blocker[2]))
        return True, f'{name} in lane'

    _clear_streak += 1
    if _clear_streak >= _FRAMES_TO_CLEAR:
        _state = _STATE_FOLLOWING
        _clear_streak = 0
        return False, ''
    return True, 'clearing'
