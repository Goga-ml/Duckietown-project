from typing import Tuple
import numpy as np


def get_motor_left_matrix(shape: Tuple[int, int]) -> np.ndarray:
    """Left motor weight matrix: highest at bottom-left, decreasing toward top-right."""
    rows,cols = shape
    matrix= np.zeros(shape, dtype=float)
    col_weights=np.linspace(1.0,0.0,cols)
    row_weights = np.linspace(0.5, 1.0, rows)
    matrix = np.outer(row_weights, col_weights) 
    return matrix



def get_motor_right_matrix(shape: Tuple[int, int]) -> np.ndarray:
    """Right motor weight matrix: highest at bottom-right, decreasing toward top-left."""
    rows,cols = shape
    matrix= np.zeros(shape, dtype=float)
    col_weights=np.linspace(0.0,1.0,cols)
    row_weights = np.linspace(0.5, 1.0, rows)
    matrix = np.outer(row_weights, col_weights) 
    return matrix