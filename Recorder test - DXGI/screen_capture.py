"""Screen capture using the Windows DXGI Desktop Duplication API via dxcam."""
from typing import Tuple
import dxcam
import numpy as np


class ScreenCapture:
    """Handles monitor configuration and DXGI frame grabbing from screen."""

    def __init__(self, monitor_index: int = 1):
        self.monitor_index = monitor_index
        if monitor_index < 1:
            raise ValueError("monitor_index must be 1 or greater")

        # MSS numbers displays from 1; dxcam numbers outputs from 0.
        self._camera = dxcam.create(
            output_idx=monitor_index - 1,
            output_color="BGR",
        )
        first_frame = self._camera.grab()
        if first_frame is None:
            self._camera.release()
            raise RuntimeError(f"DXGI could not capture monitor {monitor_index}")

        self.height, self.width = first_frame.shape[:2]

    @property
    def dimensions(self) -> Tuple[int, int]:
        """Returns the (width, height) tuple of the monitor."""
        return self.width, self.height

    def grab_frame(self) -> np.ndarray:
        """Capture and return one BGR frame from the selected DXGI output."""
        frame = self._camera.grab()
        if frame is None:
            raise RuntimeError("DXGI returned no frame")
        return frame

    def close(self) -> None:
        """Release the DXGI duplication resource."""
        if self._camera:
            self._camera.release()
            self._camera = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
