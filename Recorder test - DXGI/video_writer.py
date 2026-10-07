"""
Video writer utilities for creating and managing video output files.
Provides both direct and high-performance asynchronous multi-threaded VideoWriters.
"""
from datetime import datetime
from pathlib import Path
import queue
import threading
from typing import Iterable, Optional, Tuple, Union
import cv2
import numpy as np


def generate_timestamped_filename(prefix: str, extension: str = ".mp4") -> str:
    """Generates a filename with the current date and time timestamp."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = extension if extension.startswith(".") else f".{extension}"
    return f"{prefix}_{timestamp}{ext}"


def create_video_writer(
    output_path: Union[str, Path],
    fourcc_str: str,
    fps: float,
    dimensions: Tuple[int, int],
) -> cv2.VideoWriter:
    """
    Initializes a synchronous cv2.VideoWriter instance.

    :param output_path: Destination path for the video file.
    :param fourcc_str: 4-character code string, e.g. 'mp4v' or 'XVID'.
    :param fps: Target nominal frames per second.
    :param dimensions: Tuple of (width, height).
    :return: An opened cv2.VideoWriter object.
    """
    fourcc = cv2.VideoWriter_fourcc(*fourcc_str)
    return cv2.VideoWriter(str(output_path), fourcc, fps, dimensions)


class AsyncVideoWriter:
    """
    High-performance asynchronous VideoWriter that offloads video encoding
    and disk I/O to a dedicated background worker thread.
    Prevents the screen capture and GUI loop from stalling during video compression.
    """

    def __init__(
        self,
        output_path: Union[str, Path],
        fourcc_str: str,
        fps: float,
        dimensions: Tuple[int, int],
        queue_size: int = 180,
    ):
        self.output_path = Path(output_path)
        self.fourcc_str = fourcc_str
        self.fps = fps
        self.dimensions = dimensions
        self.queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self.running = True
        self._writer: Optional[cv2.VideoWriter] = None
        self._frames_written = 0

        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _worker(self) -> None:
        """Background thread executing the actual cv2.VideoWriter encoding calls."""
        fourcc = cv2.VideoWriter_fourcc(*self.fourcc_str)
        self._writer = cv2.VideoWriter(str(self.output_path), fourcc, self.fps, self.dimensions)

        while self.running or not self.queue.empty():
            try:
                frame = self.queue.get(timeout=0.03)
                if self._writer and self._writer.isOpened():
                    self._writer.write(frame)
                    self._frames_written += 1
                self.queue.task_done()
            except queue.Empty:
                pass

        if self._writer:
            self._writer.release()
            self._writer = None

    def write(self, frame: np.ndarray) -> None:
        """Pushes a frame into the background worker queue. Non-blocking."""
        if not self.running:
            return
        try:
            self.queue.put_nowait(frame)
        except queue.Full:
            # If queue is momentarily saturated, wait with a short timeout to prevent runaway memory
            try:
                self.queue.put(frame, timeout=0.02)
            except queue.Full:
                pass

    def release(self) -> None:
        """Signals the worker thread to finish remaining queued frames and release resources."""
        self.running = False
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

    @property
    def frames_written(self) -> int:
        return self._frames_written


def write_frames_to_file(
    output_path: Union[str, Path],
    frames: Iterable[np.ndarray],
    fourcc_str: str,
    fps: float,
    dimensions: Tuple[int, int],
) -> None:
    """Writes a sequence of frames directly to a file and releases the writer."""
    writer = create_video_writer(output_path, fourcc_str, fps, dimensions)
    try:
        for frame in frames:
            writer.write(frame)
    finally:
        writer.release()
