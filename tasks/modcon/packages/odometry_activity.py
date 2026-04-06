from typing import Tuple
import numpy as np


def delta_phi(ticks: int, prev_ticks: int, resolution: int) -> Tuple[float, int]:
    alpha = 2.0 * np.pi / resolution
    delta_ticks = ticks - prev_ticks
    dphi = delta_ticks * alpha
    return dphi, ticks


def pose_estimation(
    R: float,
    baseline: float,
    x_prev: float,
    y_prev: float,
    theta_prev: float,
    delta_phi_left: float,
    delta_phi_right: float,
) -> Tuple[float, float, float]:
    # Distance travelled by each wheel
    d_left = R * delta_phi_left
    d_right = R * delta_phi_right

    # Distance travelled by the robot (midpoint) and rotation
    d_A = (d_left + d_right) / 2.0
    delta_theta = (d_right - d_left) / baseline

    # Update pose in the world frame
    theta_new = theta_prev + delta_theta
    x_new = x_prev + d_A * np.cos(theta_prev)
    y_new = y_prev + d_A * np.sin(theta_prev)

    return x_new, y_new, theta_new
