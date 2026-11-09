"""
On-screen display and overlay module for rendering performance metrics.

- FPSCounter: smoothed frame-rate measurement, optionally burned into recorded frames.
- LiveOverlay: a small always-on-top window showing live stats on the user's monitor only.
  It is excluded from capture with WDA_EXCLUDEFROMCAPTURE, so neither DXGI Desktop
  Duplication nor Windows.Graphics.Capture ever sees it. If Windows doesn't confirm the
  exclusion, the overlay refuses to show.
"""
import ctypes
from ctypes import wintypes
import threading
import time
from typing import Callable, List, Optional, Tuple

import cv2
import numpy as np

import config
import win32_api


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
        self._window_frames = 0

    @property
    def current_fps(self) -> float:
        """Returns the current smoothed FPS value for backward compatibility."""
        return self.smoothed_fps

    def update(self) -> float:
        """
        Counts one captured frame. The displayed rate is the exact number of frames counted
        over each refresh window divided by its length, so it matches what is really captured
        (averaging 1/frame-time instead is skewed by uneven frame pacing).
        """
        current_time = time.perf_counter()
        elapsed = current_time - self.prev_time
        self.prev_time = current_time
        if elapsed > 0:
            instant_fps = 1.0 / elapsed
            if self.smoothed_fps == 0.0:
                self.smoothed_fps = instant_fps
            else:
                self.smoothed_fps = (self.alpha * instant_fps) + ((1.0 - self.alpha) * self.smoothed_fps)

        self._window_frames += 1
        window = current_time - self.last_display_update
        if window >= self.refresh_rate:
            self.display_fps = self._window_frames / window
            self._window_frames = 0
            self.last_display_update = current_time

        return self.display_fps

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


class OverlayStats:
    """
    Values the main loop publishes for the overlay thread. Each is a single attribute
    write, which is atomic under the GIL, so no lock is needed.
    """

    __slots__ = ("fps", "target_fps", "source", "state", "elapsed", "hotkeys", "source_hwnd", "monitor_device")

    def __init__(self):
        self.fps = 0.0
        self.target_fps = 0
        self.source = ""
        self.state = "IDLE"
        self.elapsed = 0.0
        self.hotkeys: Tuple[str, ...] = ()
        self.source_hwnd: Optional[int] = None
        self.monitor_device: Optional[str] = None


def overlay_line_count(visible_stats: dict, hotkey_lines: int) -> int:
    """How many text lines the overlay shows for these settings."""
    count = sum(1 for key in ("fps", "source", "elapsed") if visible_stats.get(key))
    return count + (hotkey_lines if visible_stats.get("hotkeys") else 0)


def overlay_size(line_count: int) -> Tuple[int, int]:
    """The overlay window's (width, height) in physical pixels."""
    return LiveOverlay.WIDTH, LiveOverlay.PADDING * 2 + LiveOverlay.LINE_HEIGHT * max(1, line_count)


def resolve_monitor_rect(monitor_mode: str, source_hwnd: Optional[int],
                         monitor_device: Optional[str]) -> Tuple[int, int, int, int]:
    """
    The (left, top, right, bottom) of the monitor the overlay belongs on. Call it from a
    per-monitor-DPI-aware thread (or inside win32_api.per_monitor_dpi()) for physical pixels.
    """
    monitor = None
    if monitor_mode == "source":
        if source_hwnd and win32_api.is_window(source_hwnd):
            monitor = win32_api.monitor_for_window(source_hwnd)
        elif monitor_device:
            monitor = next((m for m in win32_api.list_monitors() if m["device"] == monitor_device), None)
    if monitor is None:
        monitors = win32_api.list_monitors()
        monitor = next((m for m in monitors if m["primary"]), monitors[0] if monitors else None)
    return monitor["rect"] if monitor else (0, 0, 1920, 1080)


def overlay_origin(position: str, custom_xy: Tuple[int, int], monitor_rect: Tuple[int, int, int, int],
                   size: Tuple[int, int]) -> Tuple[int, int]:
    """
    Top-left corner of the overlay. "custom" uses X/Y offsets from the monitor's top-left,
    kept on screen; the other positions are the four corners.
    """
    left, top, right, bottom = monitor_rect
    width, height = size
    margin = LiveOverlay.MARGIN
    if position == "custom":
        x = left + max(0, min(int(custom_xy[0]), right - left - width))
        y = top + max(0, min(int(custom_xy[1]), bottom - top - height))
        return x, y
    x = left + margin if position.endswith("left") else right - width - margin
    y = top + margin if position.startswith("top") else bottom - height - margin
    return x, y


class LiveOverlay:
    """Click-through stats window on its own thread, redrawn a few times a second."""

    WIDTH = 300
    PADDING = 10
    LINE_HEIGHT = 22
    MARGIN = 16
    FONT_SCALE = 0.48
    BACKGROUND_ALPHA = 170
    CLASS_NAME = "RecorderStudioLiveOverlay"

    def __init__(self, stats: OverlayStats, on_error: Callable[[str], None]):
        self.stats = stats
        self._on_error = on_error
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.visible_stats = dict(config.OVERLAY_STATS)
        self.position = config.OVERLAY_POSITION
        self.custom_xy = (config.OVERLAY_X, config.OVERLAY_Y)
        self.monitor_mode = config.OVERLAY_MONITOR
        self._settings_changed = True

    # --- Control (main thread) ------------------------------------------------------

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="LiveOverlay")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def apply_settings(self, visible_stats: dict, position: str, monitor_mode: str,
                       custom_xy: Tuple[int, int] = (0, 0)) -> None:
        self.visible_stats = dict(visible_stats)
        self.position = position
        self.custom_xy = (int(custom_xy[0]), int(custom_xy[1]))
        self.monitor_mode = monitor_mode
        self._settings_changed = True

    # --- Overlay thread -------------------------------------------------------------

    def _run(self) -> None:
        if win32_api.SetThreadDpiAwarenessContext is not None:
            # Physical-pixel coordinates and a crisp, unscaled bitmap on high-DPI monitors.
            win32_api.SetThreadDpiAwarenessContext(win32_api.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)

        hinstance = win32_api.GetModuleHandleW(None)
        self._wndproc = win32_api.WNDPROC(self._window_proc)  # Kept alive for the window's lifetime
        window_class = win32_api.WNDCLASSEXW()
        window_class.cbSize = ctypes.sizeof(win32_api.WNDCLASSEXW)
        window_class.lpfnWndProc = self._wndproc
        window_class.hInstance = hinstance
        window_class.lpszClassName = self.CLASS_NAME
        if not win32_api.RegisterClassExW(ctypes.byref(window_class)):
            self._fail(f"Overlay could not start (window class error {ctypes.get_last_error()}).")
            return

        ex_style = (
            win32_api.WS_EX_LAYERED | win32_api.WS_EX_TRANSPARENT | win32_api.WS_EX_TOPMOST
            | win32_api.WS_EX_TOOLWINDOW | win32_api.WS_EX_NOACTIVATE
        )
        hwnd = win32_api.CreateWindowExW(
            ex_style, self.CLASS_NAME, "Recorder Overlay", win32_api.WS_POPUP,
            0, 0, self.WIDTH, self.LINE_HEIGHT, None, None, hinstance, None,
        )
        if not hwnd:
            win32_api.UnregisterClassW(self.CLASS_NAME, hinstance)
            self._fail(f"Overlay could not start (window error {ctypes.get_last_error()}).")
            return

        surface = None
        try:
            # Fail closed: never show the overlay unless Windows confirms it is hidden from capture.
            if not win32_api.exclude_from_capture(hwnd):
                self._fail("Overlay turned off: Windows couldn't exclude it from capture, so it could appear in recordings.")
                return

            surface = _LayeredSurface()
            shown = False
            last_drawn = None
            interval_ms = max(1, int(1000 / config.OVERLAY_REFRESH_HZ))
            message = wintypes.MSG()

            while not self._stop.is_set():
                while win32_api.PeekMessageW(ctypes.byref(message), None, 0, 0, win32_api.PM_REMOVE):
                    win32_api.TranslateMessage(ctypes.byref(message))
                    win32_api.DispatchMessageW(ctypes.byref(message))

                lines = self._lines()
                position = self._window_position(len(lines))
                state = (tuple(lines), position)
                if state != last_drawn or self._settings_changed:
                    self._settings_changed = False
                    last_drawn = state
                    surface.present(hwnd, self._draw(lines), position)
                    if not shown:
                        win32_api.ShowWindow(hwnd, win32_api.SW_SHOWNOACTIVATE)
                        shown = True

                win32_api.MsgWaitForMultipleObjects(0, None, False, interval_ms, win32_api.QS_ALLINPUT)
        except Exception as e:
            self._fail(f"Overlay stopped: {e}")
        finally:
            if surface is not None:
                surface.release()
            win32_api.DestroyWindow(hwnd)
            win32_api.UnregisterClassW(self.CLASS_NAME, hinstance)

    @staticmethod
    def _window_proc(hwnd, message, wparam, lparam):
        return win32_api.DefWindowProcW(hwnd, message, wparam, lparam)

    def _fail(self, message: str) -> None:
        print(f"Warning: {message}")
        self._on_error(message)

    def _lines(self) -> List[str]:
        stats = self.stats
        visible = self.visible_stats
        lines: List[str] = []
        if visible.get("fps"):
            lines.append(f"FPS  {stats.fps:5.1f} / {stats.target_fps}")
        if visible.get("source"):
            source = stats.source if len(stats.source) <= 30 else stats.source[:27] + "..."
            lines.append(f"SRC  {source}")
        if visible.get("elapsed"):
            if stats.state == "IDLE":
                lines.append("IDLE")
            else:
                seconds = int(stats.elapsed)
                lines.append(f"{'REC' if stats.state == 'RECORDING' else 'PAUSED'}  "
                             f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}")
        if visible.get("hotkeys"):
            lines.extend(stats.hotkeys)
        return lines

    def _height(self, line_count: int) -> int:
        return overlay_size(line_count)[1]

    def _window_position(self, line_count: int) -> Tuple[int, int]:
        rect = resolve_monitor_rect(self.monitor_mode, self.stats.source_hwnd, self.stats.monitor_device)
        return overlay_origin(self.position, self.custom_xy, rect, overlay_size(line_count))

    def _draw(self, lines: List[str]) -> np.ndarray:
        """Draws the stats as premultiplied BGRA, the format UpdateLayeredWindow expects."""
        image = np.zeros((self._height(len(lines)), self.WIDTH, 4), dtype=np.uint8)
        if not lines:
            return image  # Fully transparent
        # Black has premultiplied colour 0 at any alpha, so the background is just alpha.
        image[:, :, 3] = self.BACKGROUND_ALPHA
        for index, line in enumerate(lines):
            baseline = self.PADDING + (index + 1) * self.LINE_HEIGHT - 6
            cv2.putText(image, line, (self.PADDING, baseline), cv2.FONT_HERSHEY_SIMPLEX,
                        self.FONT_SCALE, (245, 245, 245, 255), 1, cv2.LINE_AA)
        return image


class _LayeredSurface:
    """A memory DC and 32-bit DIB section used to push pixels to the layered window."""

    def __init__(self):
        self._dc = win32_api.CreateCompatibleDC(None)
        self._bitmap = None
        self._old_bitmap = None
        self._bits = ctypes.c_void_p()
        self._size: Tuple[int, int] = (0, 0)
        self._blend = win32_api.BLENDFUNCTION(win32_api.AC_SRC_OVER, 0, 255, win32_api.AC_SRC_ALPHA)

    def _ensure_size(self, width: int, height: int) -> None:
        if (width, height) == self._size:
            return
        self._release_bitmap()
        info = win32_api.BITMAPINFO()
        header = info.bmiHeader
        header.biSize = ctypes.sizeof(win32_api.BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # Negative = top-down rows, matching numpy
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = win32_api.BI_RGB
        self._bitmap = win32_api.CreateDIBSection(
            self._dc, ctypes.byref(info), win32_api.DIB_RGB_COLORS, ctypes.byref(self._bits), None, 0
        )
        if not self._bitmap:
            raise OSError(f"CreateDIBSection failed ({ctypes.get_last_error()})")
        self._old_bitmap = win32_api.SelectObject(self._dc, self._bitmap)
        self._size = (width, height)

    def present(self, hwnd, image: np.ndarray, position: Tuple[int, int]) -> None:
        height, width = image.shape[:2]
        self._ensure_size(width, height)
        ctypes.memmove(self._bits, np.ascontiguousarray(image).ctypes.data, image.nbytes)

        screen_dc = win32_api.GetDC(None)
        try:
            win32_api.UpdateLayeredWindow(
                hwnd, screen_dc,
                ctypes.byref(wintypes.POINT(*position)), ctypes.byref(wintypes.SIZE(width, height)),
                self._dc, ctypes.byref(wintypes.POINT(0, 0)), 0,
                ctypes.byref(self._blend), win32_api.ULW_ALPHA,
            )
        finally:
            win32_api.ReleaseDC(None, screen_dc)

    def _release_bitmap(self) -> None:
        if self._bitmap:
            win32_api.SelectObject(self._dc, self._old_bitmap)
            win32_api.DeleteObject(self._bitmap)
            self._bitmap = None
            self._size = (0, 0)

    def release(self) -> None:
        self._release_bitmap()
        if self._dc:
            win32_api.DeleteDC(self._dc)
            self._dc = None
