"""
Capture sources for the recorder.

- DxgiMonitorSource: a whole display through DXGI Desktop Duplication (dxcam). Every new
  frame Windows presents is delivered with its present timestamp (see the class).
- WgcWindowSource: one application window through Windows.Graphics.Capture
  (windows-capture), paced by a deadline sleep.
- CaptureCompositor: fits whichever source is active onto a fixed-size canvas, so the
  encoder never sees a resolution change and sources can switch mid-recording.

Only the active source runs, so unused sources cost nothing.
"""
import importlib.util
import re
import threading
import time
import warnings
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

import config
import win32_api

# How often an active window source checks whether its window was minimized.
MINIMIZED_CHECK_SECONDS = 0.5
# Wait between attempts to restart display capture after dxcam's capture thread fails.
CAMERA_RETRY_SECONDS = 1.0
# dxcam asks Windows for a new frame this many times per display refresh. Polling at the refresh rate itself aliases against it (ticks landing just before a refresh find nothing new), which loses up to half the frames; twice the rate catches every one.
POLLS_PER_REFRESH = 2
MAX_POLL_HZ = 500
# A frame timestamp further than this from "now" is treated as unreliable.
MAX_TIMESTAMP_SKEW_SECONDS = 0.5
# While the screen doesn't change, the last image is passed on again this often. Recordings store an unchanged image once anyway; this only keeps the timeline running well below AV_GAP_CUT_SECONDS, so a still screen is never mistaken for a pause and cut.
REPEAT_AFTER_SECONDS = 0.1

warnings.filterwarnings("ignore", category=FutureWarning, module=r"dxcam(\..*)?$")
warnings.filterwarnings("ignore", message=r".*running 'comtypes' on Python", category=FutureWarning)

# Attempt to import dxcam; if unavailable, raise a clear error.
try:
    import dxcam
except ImportError as e:
    raise ImportError("dxcam is required for DXGI capture. Install it via 'pip install dxcam'.") from e

_OUTPUT_INFO_PATTERN = re.compile(
    r"Device\[(\d+)\] Output\[(\d+)\]: Res:\((\d+), (\d+)\).*Primary:(True|False)"
)

_default_thread_excepthook = threading.excepthook


def _report_capture_thread_error(args) -> None:
    """dxcam re-raises DXGI errors on its capture thread, which would print a long traceback.
    DxgiMonitorSource restarts the camera itself, so one line is enough."""
    if args.thread is not None and args.thread.name == "DXCamera":
        print(f"Warning: display capture error ({args.exc_type.__name__}: {args.exc_value}). Restarting capture.")
        return
    _default_thread_excepthook(args)


threading.excepthook = _report_capture_thread_error

# dxcam cameras are expensive to create (dxcam sleeps 0.1 s per create), so they are kept.
_cameras: Dict[Tuple[int, int], object] = {}


def list_displays() -> List[Dict]:
    """Returns every display dxcam can capture, with a label for the UI."""
    displays = []
    for line in dxcam.output_info().splitlines():
        match = _OUTPUT_INFO_PATTERN.search(line)
        if not match:
            continue
        device_idx, output_idx, width, height = (int(match.group(i)) for i in range(1, 5))
        primary = match.group(5) == "True"
        displays.append({
            "device_idx": device_idx,
            "output_idx": output_idx,
            "width": width,
            "height": height,
            "primary": primary,
        })
    for number, display in enumerate(displays, start=1):
        suffix = ", primary" if display["primary"] else ""
        display["label"] = f"Display {number} ({display['width']}x{display['height']}{suffix})"
    return displays


def primary_display() -> Dict:
    displays = list_displays()
    if not displays:
        raise RuntimeError("dxcam found no displays to capture.")
    return next((d for d in displays if d["primary"]), displays[0])


def window_capture_available() -> bool:
    """True if windows-capture is installed. Checked without importing it."""
    return importlib.util.find_spec("windows_capture") is not None


def _boost_capture_threads() -> None:
    """Runs dxcam's capture thread above normal priority, so CPU-heavy encoders (x264) can't
    delay frame grabs: a grab that misses its timer tick is a frame lost from capture."""
    for thread in threading.enumerate():
        if thread.name == "DXCamera" and thread.is_alive():
            win32_api.set_thread_priority(thread.native_id, win32_api.THREAD_PRIORITY_HIGHEST)


def _get_camera(device_idx: int, output_idx: int):
    key = (device_idx, output_idx)
    camera = _cameras.get(key)
    if camera is None:
        # BGR output matches the rest of the pipeline.
        camera = dxcam.create(device_idx=device_idx, output_idx=output_idx, output_color="BGR")
        _cameras[key] = camera
    return camera


class DxgiMonitorSource:
    """
    Captures one whole display.

    dxcam polls Desktop Duplication at twice the display's refresh rate and keeps only frames
    that changed, so every refresh the screen actually shows is caught. next_frame() returns
    each new frame with the time Windows presented it (QPC, the same clock as
    time.perf_counter()), so frames are placed in recordings by when they were on screen,
    not by when this thread happened to pick them up. Frames are capped at the target rate:
    one per output frame slot. While the screen doesn't change, the last frame is repeated
    once per slot, so recordings keep running in real time.
    """

    kind = "display"

    def __init__(self, display: Dict, fps: int):
        self.display = display
        self.label = display["label"]
        self.hwnd = None
        self._fps = fps
        self._camera = _get_camera(display["device_idx"], display["output_idx"])
        self._running = False
        self._retry_at = 0.0
        self._frame_event = None
        self._reset_timing()

    def _reset_timing(self) -> None:
        self._held: Optional[np.ndarray] = None
        self._last_ts: Optional[float] = None       # Time of the last frame returned
        self._last_slot: Optional[int] = None       # Its slot on the target-rate grid
        self._slot_origin: Optional[float] = None

    @property
    def key(self) -> Tuple:
        return ("display", self.display["device_idx"], self.display["output_idx"])

    @property
    def monitor_device(self) -> Optional[str]:
        output = getattr(self._camera, "_output", None)
        return getattr(output, "devicename", None)

    @property
    def closed(self) -> bool:
        return False

    minimized = False
    keep_restored = False

    def _poll_rate(self) -> int:
        refresh = win32_api.display_refresh_rate(self.monitor_device) or 60
        return int(min(MAX_POLL_HZ, max(refresh * POLLS_PER_REFRESH, self._fps)))

    def _start_camera(self) -> None:
        # video_mode=False: dxcam publishes only frames that changed; repeats are made here.
        self._camera.start(target_fps=self._poll_rate(), video_mode=False)
        # dxcam signals each new frame with this event (it has no public wait-with-timeout).
        self._frame_event = getattr(self._camera, "_DXCamera__frame_available", None)
        _boost_capture_threads()

    def start(self) -> None:
        if not self._running:
            self._reset_timing()
            self._start_camera()
            self._running = True

    def stop(self) -> None:
        if self._running:
            self._running = False
            try:
                self._camera.stop()
            except Exception as e:  # A camera whose thread already failed
                print(f"Warning: error while stopping display capture: {e}")

    def set_fps(self, fps: int) -> None:
        self._fps = fps
        if self._running:
            self.stop()
            self.start()

    def _wait_for_new_frame(self, timeout: float) -> bool:
        event = self._frame_event
        if event is not None:
            return event.wait(timeout) if timeout > 0 else event.is_set()
        if timeout <= 0:
            return False
        # Fallback for dxcam versions without the event: poll its frame counter.
        start_ticks = self._camera.latest_frame_ticks
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self._camera.latest_frame_ticks != start_ticks:
                return True
            time.sleep(0.001)
        return False

    def next_frame(self) -> Tuple[Optional[np.ndarray], float]:
        """(frame, capture time): the next new screen image, or, if the screen hasn't changed
        for REPEAT_AFTER_SECONDS, the last one again (so recordings keep running in real time)."""
        repeat_after = max(1.0 / self._fps, REPEAT_AFTER_SECONDS)
        now = time.perf_counter()
        if self._last_ts is None or now - self._last_ts >= 1.0:
            deadline = now + repeat_after
        else:
            deadline = max(self._last_ts + repeat_after, now)
        while True:
            if not self._camera.is_capturing:
                if self._running:
                    self._recover()
                return self._held, time.perf_counter()
            if not self._wait_for_new_frame(deadline - time.perf_counter()):
                # Nothing changed on screen for a whole slot: repeat the last frame.
                now = time.perf_counter()
                if self._held is not None:
                    self._accept(now)
                return self._held, now
            result = self._camera.get_latest_frame(with_timestamp=True)
            if result is None:
                continue
            frame, ts = result
            now = time.perf_counter()
            if not ts or abs(ts - now) > MAX_TIMESTAMP_SKEW_SECONDS:
                ts = now
            if self._last_ts is not None and ts <= self._last_ts:
                ts = min(now, self._last_ts + 1e-4)
            if self._slot_origin is None:
                self._slot_origin = ts
            if self._last_slot is not None and self._slot_of(ts) <= self._last_slot:
                # The display refreshes faster than the target rate: one frame per slot.
                self._held = frame
                continue
            self._held = frame
            self._accept(ts)
            return frame, ts

    def _slot_of(self, ts: float) -> int:
        # Nearest slot, not the one below: a refresh a fraction of a millisecond early must
        # still count as the next frame (rounding down would merge it with the previous one).
        return int(round((ts - self._slot_origin) * self._fps))

    def _accept(self, ts: float) -> None:
        if self._slot_origin is None:
            self._slot_origin = ts
        self._last_ts = ts
        self._last_slot = self._slot_of(ts)

    def _recover(self) -> None:
        """
        dxcam's capture thread ends on an unexpected DXGI error (e.g. DXGI_ERROR_INVALID_CALL
        after a display mode change, a full-screen app or the secure desktop), leaving the
        camera unusable. A fresh camera is created and started; until that works, the loop
        is paced here and the last frame is held, so recordings stay in sync.
        """
        now = time.perf_counter()
        if now >= self._retry_at:
            self._retry_at = now + CAMERA_RETRY_SECONDS
            key = (self.display["device_idx"], self.display["output_idx"])
            try:
                try:
                    self._camera.stop()
                except Exception:
                    pass
                try:
                    self._camera.release()
                except Exception:
                    pass
                _cameras.pop(key, None)
                self._camera = _get_camera(*key)
                self._start_camera()
                print(f"Display capture restarted on {self.label}.")
                return
            except Exception as e:
                print(f"Warning: display capture stopped ({e}); retrying in {CAMERA_RETRY_SECONDS:.0f} s.")
        time.sleep(1.0 / max(1, self._fps))


class WgcWindowSource:
    """
    Captures one window's own content, even when other windows cover it.

    Windows stops rendering a window while it is minimized, so no capture API (WGC,
    PrintWindow, DWM thumbnails) gets new frames from it. The source therefore restores a
    minimized window without activating it when capture starts, and keeps it restored
    while `keep_restored` is set (during recording). Otherwise the last frame is held and
    `minimized` tells the UI why the feed is frozen.
    """

    kind = "window"

    def __init__(self, hwnd: int, title: str, fps: int):
        self.hwnd = hwnd
        self.label = title
        self.display = None
        self._fps = fps
        self._interval = 1.0 / fps
        self._deadline: Optional[float] = None
        self._latest: Optional[Tuple[np.ndarray, float]] = None  # (frame, arrival time)
        self._returned = None
        self._closed = threading.Event()
        self._control = None
        self._stopping = False
        self.minimized = False
        self.keep_restored = False
        self._next_minimized_check = 0.0

    @property
    def key(self) -> Tuple:
        return ("window", self.hwnd)

    def ensure_restored(self) -> None:
        """Un-minimizes the window (without focusing it) if it is minimized."""
        if win32_api.is_minimized(self.hwnd):
            win32_api.restore_without_activating(self.hwnd)
        self.minimized = False
        self._next_minimized_check = time.perf_counter() + MINIMIZED_CHECK_SECONDS

    def _check_minimized(self, now: float) -> None:
        self._next_minimized_check = now + MINIMIZED_CHECK_SECONDS
        minimized = win32_api.is_minimized(self.hwnd)
        if minimized and self.keep_restored:
            win32_api.restore_without_activating(self.hwnd)
            minimized = False
        self.minimized = minimized

    @property
    def monitor_device(self) -> Optional[str]:
        return None

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def _build_capture(self):
        from windows_capture import WindowsCapture

        options = dict(
            cursor_capture=True,
            minimum_update_interval=max(1, int(1000 / self._fps)),
            window_hwnd=self.hwnd,
        )
        try:
            capture = WindowsCapture(draw_border=False, **options)
        except Exception:
            # Hiding the capture border isn't supported on every Windows build.
            capture = WindowsCapture(**options)

        source = self

        @capture.event
        def on_frame_arrived(frame, capture_control):
            if source._stopping:
                capture_control.stop()
                return
            # cvtColor writes a new array, copying the frame out of the native buffer
            # before windows-capture reuses it.
            image = cv2.cvtColor(frame.frame_buffer, cv2.COLOR_BGRA2BGR)
            source._latest = (image, time.perf_counter())

        @capture.event
        def on_closed():
            source._closed.set()

        return capture

    def start(self) -> None:
        if self._control is None:
            if config.RESTORE_MINIMIZED_WINDOW_ON_SELECT or self.keep_restored:
                # A minimized window produces no frames at all, so the feed would never start.
                self.ensure_restored()
            self._stopping = False
            self._control = self._build_capture().start_free_threaded()
            self._deadline = None

    def stop(self) -> None:
        if self._control is not None:
            self._stopping = True
            try:
                self._control.stop()
            except Exception as e:
                print(f"Warning: error while stopping window capture: {e}")
            self._control = None

    def set_fps(self, fps: int) -> None:
        self._fps = fps
        self._interval = 1.0 / fps
        if self._control is not None:
            self.stop()
            self.start()

    def next_frame(self) -> Tuple[Optional[np.ndarray], float]:
        """(frame, capture time): a new frame with its arrival time, or the last one repeated now."""
        # Deadline pacing: each deadline advances by one interval, so sleep jitter
        # doesn't accumulate. timeBeginPeriod(1) in main.py keeps the sleeps precise.
        now = time.perf_counter()
        if self._deadline is None or now - self._deadline > self._interval:
            self._deadline = now  # First frame, or fell behind: resync instead of bursting.
        else:
            self._deadline += self._interval
            delay = self._deadline - now
            if delay > 0:
                time.sleep(delay)
        if now >= self._next_minimized_check:
            self._check_minimized(now)  # One cheap IsIconic call every half second
        # A window that isn't updating (e.g. minimized) keeps its last frame.
        latest = self._latest
        if latest is None:
            return None, time.perf_counter()
        if latest is self._returned:
            return latest[0], time.perf_counter()
        self._returned = latest
        frame, arrived = latest
        now = time.perf_counter()
        return frame, arrived if 0 <= now - arrived < MAX_TIMESTAMP_SKEW_SECONDS else now


class CaptureCompositor:
    """Owns the active capture source and fits its frames onto a fixed-size canvas."""

    def __init__(self, fps: int, canvas_size: Optional[Tuple[int, int]] = None):
        self.fps = fps
        primary = primary_display()
        self.native_size: Tuple[int, int] = (primary["width"], primary["height"])
        self.canvas_size: Tuple[int, int] = canvas_size or self.native_size
        self._black = np.zeros((self.canvas_size[1], self.canvas_size[0], 3), dtype=np.uint8)
        self._last_output: Optional[np.ndarray] = None
        self._last_source_frame: Optional[np.ndarray] = None
        self._layouts: Dict[Tuple[int, int], Tuple[int, int, int, int, int]] = {}
        self._notice: Optional[str] = None
        self._keep_window_restored = False

        self._last_display: Dict = primary
        self._source = DxgiMonitorSource(primary, fps)
        self._source.start()

    # --- Source information ---------------------------------------------------------

    @property
    def kind(self) -> str:
        return self._source.kind

    @property
    def label(self) -> str:
        return self._source.label

    @property
    def source_key(self) -> Tuple:
        return self._source.key

    @property
    def source_hwnd(self) -> Optional[int]:
        return self._source.hwnd

    @property
    def monitor_device(self) -> Optional[str]:
        return self._source.monitor_device

    @property
    def window_minimized(self) -> bool:
        """True while the captured window is minimized (its feed is frozen)."""
        return self._source.minimized

    def set_keep_window_restored(self, keep: bool) -> None:
        """While set (during recording), a captured window is un-minimized automatically."""
        self._keep_window_restored = keep and config.KEEP_CAPTURED_WINDOW_RESTORED_WHILE_RECORDING
        self._source.keep_restored = self._keep_window_restored
        if self._keep_window_restored and self._source.kind == "window":
            self._source.ensure_restored()

    def take_notice(self) -> Optional[str]:
        """Returns (once) a message about a source change the app didn't request."""
        notice, self._notice = self._notice, None
        return notice

    # --- Switching ------------------------------------------------------------------

    def _switch(self, new_source) -> None:
        old_source = self._source
        new_source.keep_restored = self._keep_window_restored
        old_source.stop()
        try:
            new_source.start()
        except Exception:
            old_source.start()
            raise
        self._source = new_source

    def select_display(self, display: Dict) -> Optional[str]:
        """Switches to a display. Returns an error message, or None on success."""
        if self._source.key == ("display", display["device_idx"], display["output_idx"]):
            return None
        try:
            self._switch(DxgiMonitorSource(display, self.fps))
        except Exception as e:
            return f"Could not capture {display['label']}: {e}"
        self._last_display = display
        return None

    def select_window(self, hwnd: int, title: str) -> Optional[str]:
        """Switches to a window. Returns an error message, or None on success."""
        if not window_capture_available():
            return "Window capture needs the windows-capture package (pip install windows-capture)."
        if not win32_api.is_window(hwnd):
            return "That window no longer exists."
        if self._source.key == ("window", hwnd):
            return None
        try:
            self._switch(WgcWindowSource(hwnd, title, self.fps))
        except Exception as e:
            return f"Could not capture window '{title}': {e}"
        return None

    def restore_display(self) -> None:
        """Returns to the display that was active before window capture."""
        if self._source.kind != "display":
            error = self.select_display(self._last_display)
            if error:
                self.select_display(primary_display())

    def set_fps(self, fps: int) -> None:
        self.fps = fps
        self._source.set_fps(fps)

    def set_canvas_size(self, size: Tuple[int, int]) -> None:
        """Changes the output resolution. Frames are scaled here once, before every consumer."""
        size = (int(size[0]), int(size[1]))
        if size == self.canvas_size:
            return
        self.canvas_size = size
        self._black = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        self._layouts.clear()
        self._last_output = None
        self._last_source_frame = None

    # --- Frames ---------------------------------------------------------------------

    def next_frame(self) -> Tuple[np.ndarray, float]:
        """
        (canvas-sized frame, capture time). The source paces this to at most the target FPS.
        The capture time is when the image was on screen (perf_counter clock), which is what
        recordings use to place it.
        """
        source = self._source
        if source.closed:
            self._notice = f"Window '{source.label}' closed. Back to {self._last_display['label']}."
            self.restore_display()
            source = self._source

        frame, capture_time = source.next_frame()
        if frame is None:
            return (self._last_output if self._last_output is not None else self._black), capture_time
        if frame is self._last_source_frame and self._last_output is not None:
            return self._last_output, capture_time  # A repeat: already fitted
        output = self._fit(frame)
        self._last_source_frame = frame
        self._last_output = output
        return output, capture_time

    def _fit(self, frame: np.ndarray) -> np.ndarray:
        height, width = frame.shape[:2]
        canvas_w, canvas_h = self.canvas_size
        if (width, height) == (canvas_w, canvas_h):
            return frame  # Same size: no copy at all.

        layout = self._layouts.get((width, height))
        if layout is None:
            scale = min(canvas_w / width, canvas_h / height)
            scaled_w = max(1, min(canvas_w, round(width * scale)))
            scaled_h = max(1, min(canvas_h, round(height * scale)))
            x = (canvas_w - scaled_w) // 2
            y = (canvas_h - scaled_h) // 2
            # INTER_LINEAR is the fastest good-quality filter (SIMD, multithreaded in OpenCV).
            smooth = scale < 1 and config.SCALE_INTERPOLATION == "area"
            interpolation = cv2.INTER_AREA if smooth else cv2.INTER_LINEAR
            layout = (x, y, scaled_w, scaled_h, interpolation)
            self._layouts[(width, height)] = layout
        x, y, scaled_w, scaled_h, interpolation = layout

        resized = cv2.resize(frame, (scaled_w, scaled_h), interpolation=interpolation)
        if (scaled_w, scaled_h) == (canvas_w, canvas_h):
            return resized  # Same aspect ratio (e.g. 1440p -> 1080p): no bars, no extra copy
        # A new array per frame: the writer queue and replay buffer keep references.
        output = np.empty((canvas_h, canvas_w, 3), dtype=np.uint8)
        if scaled_h < canvas_h:
            output[:y] = 0
            output[y + scaled_h:] = 0
        if scaled_w < canvas_w:
            output[y:y + scaled_h, :x] = 0
            output[y:y + scaled_h, x + scaled_w:] = 0
        output[y:y + scaled_h, x:x + scaled_w] = resized
        return output

    def close(self) -> None:
        """Stops capture and releases every dxcam camera."""
        self._source.stop()
        for camera in _cameras.values():
            try:
                camera.release()
            except Exception:
                pass
        _cameras.clear()
