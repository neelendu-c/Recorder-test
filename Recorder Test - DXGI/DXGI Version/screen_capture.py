'''
DXGI screen capture module using dxcam for high-performance GPU‑accelerated capture.
'''
import numpy as np
from typing import Tuple

# Attempt to import dxcam; if unavailable, raise a clear error.
try:
    import dxcam
except ImportError as e:
    raise ImportError("dxcam is required for DXGI capture. Install it via 'pip install dxcam'.") from e


class ScreenCapture:
    """Handles monitor configuration and fast frame grabbing via DXGI (dxcam)."""

    def __init__(self, monitor_index: int = 1):
        # dxcam captures the primary monitor; monitor_index is kept for API compatibility
        self.monitor_index = monitor_index
        # Create dxcam instance with BGR output to match existing pipeline
        self._cam = dxcam.create(output_color="BGR")
        # Start the capture; target_fps=0 lets dxcam run at max FPS
        self._cam.start(target_fps=0)
        # Dimensions will be known after the first frame is captured
        self.width = None
        self.height = None

    @property
    def dimensions(self) -> Tuple[int, int]:
        """Returns (width, height) of the captured monitor.
        If dimensions are not yet known, attempts to grab a frame to infer them.
        """
        if self.width is None or self.height is None:
            # Try to obtain a frame to establish dimensions
            frame = self._cam.get_latest_frame()
            if frame is not None:
                self.height, self.width = frame.shape[:2]
            else:
                raise RuntimeError("ScreenCapture dimensions not initialized – capture a frame first.")
        return self.width, self.height

    def grab_frame(self) -> np.ndarray:
        """Grab a single frame using dxcam. Returns a BGR numpy array.

        The first call also determines the monitor resolution.
        """
        frame = self._cam.get_latest_frame()
        if frame is None:
            # If the camera hasn't produced a frame yet, wait briefly and retry
            import time
            time.sleep(0.001)
            frame = self._cam.get_latest_frame()
            if frame is None:
                raise RuntimeError("dxcam failed to provide a frame.")
        # Initialise dimensions on first successful capture
        if self.width is None or self.height is None:
            self.height, self.width = frame.shape[:2]
        return frame

    def close(self) -> None:
        """Stops and releases the dxcam instance."""
        if hasattr(self, "_cam") and self._cam is not None:
            self._cam.stop()
            self._cam = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
