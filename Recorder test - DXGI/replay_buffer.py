"""
Circular replay buffer module for saving retrospective video clips.
Supports both synchronous and non-blocking asynchronous clip exports.
"""
from collections import deque
from pathlib import Path
import threading
from typing import Callable, List, Optional, Tuple, Union
import numpy as np

from video_writer import generate_timestamped_filename, write_frames_to_file


class ReplayBuffer:
    """Manages a rolling circular buffer of recent frames for instant replay clipping."""

    def __init__(self, max_frames: int):
        self.max_frames = max_frames
        self._buffer: deque = deque(maxlen=max_frames)

    def add_frame(self, frame: np.ndarray) -> None:
        """Stores the given frame into the circular buffer."""
        self._buffer.append(frame)

    def resize(self, new_max_frames: int) -> None:
        """Dynamically adjusts the maximum capacity of the circular buffer while preserving recent frames."""
        self.max_frames = new_max_frames
        self._buffer = deque(self._buffer, maxlen=new_max_frames)

    def is_full(self) -> bool:
        """Checks whether the buffer has reached its maximum configured capacity."""
        return len(self._buffer) == self._buffer.maxlen

    def get_frames(self) -> List[np.ndarray]:
        """Returns a list snapshot of the currently buffered frames."""
        return list(self._buffer)

    def save_clip(
        self,
        output_dir: Union[str, Path],
        fps: float,
        dimensions: Tuple[int, int],
        fourcc_str: str,
        prefix: str = "clipped_30s",
    ) -> Optional[Path]:
        """
        Exports the current buffer content synchronously as a video file.

        :param output_dir: Directory where the clip file should be saved.
        :param fps: Nominal frame rate.
        :param dimensions: (width, height) of the video.
        :param fourcc_str: FourCC codec string (e.g., 'XVID').
        :param prefix: Filename prefix.
        :return: Path to the saved clip file, or None if buffer is empty or not full.
        """
        if not self.is_full():
            print("Buffer is not yet full. Cannot save complete clip.")
            return None

        filename = generate_timestamped_filename(prefix=prefix, extension=".mp4")
        save_path = Path(output_dir) / filename

        frames_snapshot = self.get_frames()
        write_frames_to_file(save_path, frames_snapshot, fourcc_str, fps, dimensions)
        return save_path

    def save_clip_async(
        self,
        output_dir: Union[str, Path],
        fps: float,
        dimensions: Tuple[int, int],
        fourcc_str: str,
        prefix: str = "clipped_30s",
        on_complete: Optional[Callable[[Path], None]] = None,
    ) -> Optional[Path]:
        """
        Exports the current buffer content asynchronously in a background worker thread
        so that live recording never drops frames during disk encoding.

        :param output_dir: Directory where the clip file should be saved.
        :param fps: Nominal frame rate.
        :param dimensions: (width, height) of the video.
        :param fourcc_str: FourCC codec string (e.g., 'XVID').
        :param prefix: Filename prefix.
        :param on_complete: Optional callback invoked with the saved Path upon completion.
        :return: Path to the anticipated clip file, or None if buffer is not full.
        """
        if not self.is_full():
            print("Buffer is not yet full. Cannot save complete clip.")
            return None

        filename = generate_timestamped_filename(prefix=prefix, extension=".mp4")
        save_path = Path(output_dir) / filename
        frames_snapshot = self.get_frames()

        def _worker():
            try:
                write_frames_to_file(save_path, frames_snapshot, fourcc_str, fps, dimensions)
                if on_complete:
                    on_complete(save_path)
            except Exception as e:
                print(f"Error asynchronously writing clip: {e}")

        threading.Thread(target=_worker, daemon=True).start()
        return save_path

    def __len__(self) -> int:
        return len(self._buffer)
