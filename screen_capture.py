"""
Screen capture module using mss for high-performance screen grabbing.
"""
from typing import Tuple
import mss
import numpy as np


class ScreenCapture:
    """Handles monitor configuration and zero-copy frame grabbing from screen."""

    def __init__(self, monitor_index: int = 1):
        self.monitor_index = monitor_index
        mss_factory = getattr(mss, "MSS", mss.mss)
        self._sct = mss_factory()
        self.monitor = self._sct.monitors[self.monitor_index]
        self.width = self.monitor["width"]
        self.height = self.monitor["height"]

    @property
    def dimensions(self) -> Tuple[int, int]:
        """Returns the (width, height) tuple of the monitor."""
        return self.width, self.height

    def grab_frame(self) -> np.ndarray:
        """
        Captures a single screen frame and returns a BGR numpy array
        using high-performance memory-mapped buffer slicing.
        """
        screenshot = self._sct.grab(self.monitor)
        # Zero-copy buffer wrap: slice BGR channels from 32-bit BGRA buffer directly
        bgra_view = np.frombuffer(screenshot.raw, dtype=np.uint8).reshape((self.height, self.width, 4))
        return bgra_view[:, :, :3]

    def close(self) -> None:
        """Closes the mss capture instance."""
        if self._sct:
            self._sct.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
