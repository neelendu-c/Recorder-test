"""
On-screen display and overlay module for rendering performance metrics.
"""
import time
from typing import Tuple
import cv2
import numpy as np


class FPSCounter:
    """Calculates and renders smoothed frames-per-second onto video frames using high-precision timers."""

    def __init__(
        self,
        font_scale: float = 0.8,
        color: Tuple[int, int, int] = (0, 255, 0),
        thickness: int = 2,
        smoothing_factor: float = 0.1,
        refresh_rate_sec: float = 0.25,
    ):
        self.font_scale = font_scale
        self.color = color
        self.thickness = thickness
        self.alpha = smoothing_factor
        self.refresh_rate = refresh_rate_sec

        self.prev_time = time.perf_counter()
        self.smoothed_fps = 0.0
        self.display_fps = 0.0
        self.last_display_update = time.perf_counter()

    @property
    def current_fps(self) -> float:
        """Returns the current smoothed FPS value for backward compatibility."""
        return self.smoothed_fps

    def update(self) -> float:
        """Updates and returns the smoothed frames-per-second calculation."""
        current_time = time.perf_counter()
        elapsed = current_time - self.prev_time
        self.prev_time = current_time

        if elapsed > 0:
            instant_fps = 1.0 / elapsed
            if self.smoothed_fps == 0.0:
                self.smoothed_fps = instant_fps
            else:
                self.smoothed_fps = (self.alpha * instant_fps) + ((1.0 - self.alpha) * self.smoothed_fps)

        if (current_time - self.last_display_update) >= self.refresh_rate:
            self.display_fps = self.smoothed_fps
            self.last_display_update = current_time

        return self.smoothed_fps

    def draw_fps(self, frame: np.ndarray, position: Tuple[int, int] = (15, 35)) -> np.ndarray:
        """Renders the current FPS text on the given frame with a black outline for contrast."""
        text = f"FPS: {self.display_fps:.1f}"
        # Dark outline for high contrast on any background
        cv2.putText(
            frame,
            text,
            position,
            cv2.FONT_HERSHEY_SIMPLEX,
            self.font_scale,
            (0, 0, 0),
            self.thickness + 2,
        )
        # Main text
        cv2.putText(
            frame,
            text,
            position,
            cv2.FONT_HERSHEY_SIMPLEX,
            self.font_scale,
            self.color,
            self.thickness,
        )
        return frame
