"""
Thumbnails and durations for the dashboard, produced on one background thread.

- Video files (Recordings / Clips): duration from the container's frame count and frame
  rate (exact for the constant-frame-rate files this app writes) and a still frame.
- Application windows: a still image via PrintWindow, or the app's icon when the window
  is minimized or can't be drawn.

The UI asks for what it is about to draw. Unknown items are queued (newest first, so the
page being looked at loads first) and the UI redraws when `version` changes. The thread
only starts on the first request, so the gallery costs nothing until it is opened.
"""
import queue
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

import cv2
import numpy as np

import win32_api

THUMB_BACKGROUND = (12, 10, 8)
ICON_SIZE = 32


def fit_thumbnail(image: np.ndarray, size: Tuple[int, int]) -> np.ndarray:
    """Scales an image into size (w, h), keeping its aspect ratio, on a dark background."""
    width, height = size
    src_h, src_w = image.shape[:2]
    scale = min(width / src_w, height / src_h)
    scaled_w = max(1, min(width, round(src_w * scale)))
    scaled_h = max(1, min(height, round(src_h * scale)))
    resized = cv2.resize(image, (scaled_w, scaled_h), interpolation=cv2.INTER_AREA)
    output = np.empty((height, width, 3), dtype=np.uint8)
    output[:] = THUMB_BACKGROUND
    x, y = (width - scaled_w) // 2, (height - scaled_h) // 2
    output[y:y + scaled_h, x:x + scaled_w] = resized
    return output


def container_duration(path: Path) -> Optional[float]:
    """The file's duration in seconds from its container (PyAV), or None if unavailable."""
    try:
        import av
        with av.open(str(path)) as container:
            if container.duration:
                return container.duration / 1_000_000  # av.time_base: microseconds
    except Exception:
        pass
    return None


def probe_video(path: Path, size: Tuple[int, int]) -> Dict:
    """Returns {"duration": seconds or None, "thumb": BGR image or None}."""
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return {"duration": None, "thumb": None}
        frames = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = capture.get(cv2.CAP_PROP_FPS)
        # Recordings store each frame once with its real timestamp (variable frame rate), so
        # the frame count no longer gives the length; the container's duration does.
        duration = container_duration(path)
        if duration is None:
            duration = frames / fps if frames > 0 and fps > 0 else None
        # A frame a little way in is more telling than the first one.
        target = int(min(frames * 0.1, fps)) if duration else 0
        if target > 0:
            capture.set(cv2.CAP_PROP_POS_FRAMES, target)
        ok, frame = capture.read()
        if not ok and target > 0:
            capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = capture.read()
        thumb = fit_thumbnail(frame, size) if ok and frame is not None else None
        return {"duration": duration, "thumb": thumb}
    finally:
        capture.release()


def window_thumbnail(hwnd: int, size: Tuple[int, int]) -> Optional[np.ndarray]:
    """A still of the window, or its icon on a tile if it can't be drawn."""
    image = win32_api.capture_window_image(hwnd)
    # Some apps answer PrintWindow with a blank image; treat that like no image.
    if image is not None and image.size and int(image.max()) > 12:
        return fit_thumbnail(image, size)
    icon = win32_api.window_icon_image(hwnd, ICON_SIZE, THUMB_BACKGROUND)
    if icon is None:
        return None
    width, height = size
    tile = np.empty((height, width, 3), dtype=np.uint8)
    tile[:] = THUMB_BACKGROUND
    side = min(ICON_SIZE, width, height)
    x, y = (width - side) // 2, (height - side) // 2
    tile[y:y + side, x:x + side] = icon[:side, :side]
    return tile


class MediaInfoCache:
    """Background loader and cache for file and window thumbnails."""

    def __init__(self, file_thumb_size: Tuple[int, int], window_thumb_size: Tuple[int, int]):
        self.file_thumb_size = file_thumb_size
        self.window_thumb_size = window_thumb_size
        self.version = 0  # Bumped whenever a result arrives
        self._files: Dict[Tuple, Dict] = {}
        self._windows: Dict[int, Optional[np.ndarray]] = {}
        self._pending = set()
        self._lock = threading.Lock()
        self._queue: queue.LifoQueue = queue.LifoQueue()
        self._thread: Optional[threading.Thread] = None

    # --- Lookups (main thread) ----------------------------------------------------

    def file_info(self, path: Path, mtime: float, size: int) -> Optional[Dict]:
        """{"duration", "thumb"} for a video file, or None while it is being read."""
        key = (str(path), mtime, size)
        info = self._files.get(key)
        if info is None:
            self._request(("file", key))
        return info

    def window_thumbnail(self, hwnd: int) -> Tuple[bool, Optional[np.ndarray]]:
        """(ready, image). A ready result can still be None if no image was available."""
        if hwnd in self._windows:
            return True, self._windows[hwnd]
        self._request(("window", hwnd))
        return False, None

    def forget_windows(self) -> None:
        """Drops window stills so the next lookups take fresh ones."""
        self._windows.clear()

    def prune_files(self, valid_keys) -> None:
        """Forgets files that no longer exist (or changed)."""
        valid = set(valid_keys)
        # list() copies the keys in one step, so the worker adding entries can't break the loop.
        for key in [k for k in list(self._files) if k not in valid]:
            self._files.pop(key, None)

    # --- Worker -------------------------------------------------------------------

    def _request(self, job: Tuple) -> None:
        with self._lock:
            if job in self._pending:
                return
            self._pending.add(job)
            if self._thread is None:
                self._thread = threading.Thread(target=self._worker, daemon=True, name="MediaInfo")
                self._thread.start()
        self._queue.put(job)

    def _worker(self) -> None:
        # Physical-pixel window sizes, so PrintWindow captures the whole window on high-DPI screens.
        if win32_api.SetThreadDpiAwarenessContext is not None:
            win32_api.SetThreadDpiAwarenessContext(win32_api.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        while True:
            job = self._queue.get()
            kind, key = job
            try:
                if kind == "file":
                    self._files[key] = probe_video(Path(key[0]), self.file_thumb_size)
                else:
                    self._windows[key] = (
                        window_thumbnail(key, self.window_thumb_size) if win32_api.is_window(key) else None
                    )
            except Exception as e:
                print(f"Warning: could not read a thumbnail: {e}")
                if kind == "file":
                    self._files[key] = {"duration": None, "thumb": None}
                else:
                    self._windows[key] = None
            with self._lock:
                self._pending.discard(job)
            self.version += 1
