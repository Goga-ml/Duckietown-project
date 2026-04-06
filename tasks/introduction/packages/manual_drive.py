from typing import Dict, Tuple
import logging
logger = logging.getLogger(__name__)

SPEED = 1
TURN = 0.5


def get_motor_speeds(keys_pressed: Dict[str, bool]) -> Tuple[float, float]:
        left = 0.0
        right = 0.0
    
        # Forward / backward
        if keys_pressed.get("up"):
            left += SPEED
            right += SPEED
        if keys_pressed.get("down"):
            left -= SPEED
            right -= SPEED

        # Turning
        if keys_pressed.get("left"):
            left -= TURN
            right += TURN
        if keys_pressed.get("right"):
            left += TURN
            right -= TURN

        logger.debug(f"Motor speeds: left={left}, right={right}")
        return left, right
