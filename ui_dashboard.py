"""
Studio UI Dashboard.

A responsive, modern dashboard drawn with OpenCV:
- The layout follows the window. Wide windows get a preview column and a control panel;
  narrow ones stack the preview above the panel. Button groups flow onto extra rows when
  space runs out, and the tab bar collapses into a MENU button with a dropdown when the
  tabs no longer fit.
- The panel's content scrolls with the mouse wheel when it is taller than the window.
- Gallery cards show a thumbnail and the exact length of each file; the window picker
  shows a still of each application. Both load on a background thread (media_info).
- Settings > Overlay has a screen map with a draggable mock of the live overlay.

Performance: the static layout is drawn once into a cached canvas and only rebuilt when
something changes (click, hover, resize, new thumbnail). Each frame only adds the live
preview, status chip, timer, gauge and toast.
"""
from datetime import datetime
import math
import os
from pathlib import Path
import subprocess
import time
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

import config
import win32_api
from hotkeys import display_name
from media_info import MediaInfoCache
from overlay import overlay_line_count, overlay_origin, overlay_size, resolve_monitor_rect
from screen_capture import list_displays, window_capture_available
from video_writer import clip_encoder_name, target_bitrate_kbps, track_label, track_sort_key

# --- Palette (BGR) -----------------------------------------------------------------------
BG = (20, 16, 13)                # #0d1014 app background
SURFACE = (31, 26, 22)           # #161a1f panels
CARD = (44, 37, 32)              # #20252c cards and buttons
CARD_HOVER = (60, 51, 44)        # #2c333c
BORDER = (68, 59, 52)            # #343b44
BORDER_STRONG = (118, 106, 96)   # #606a76
SCREEN = (12, 10, 8)             # preview / thumbnail wells
SHADOW = (5, 4, 3)

TEXT_WHITE = (250, 250, 250)
TEXT_LIGHT = (222, 216, 210)     # inactive button text (high contrast)
TEXT_GRAY = (178, 171, 164)
TEXT_DIM = (124, 117, 110)

ACCENT = (255, 140, 48)          # #308cff electric blue: active / selected
ACCENT_HOVER = (255, 166, 92)
ACCENT_SOFT = (92, 56, 28)       # #1c385c selected-option fill
ACCENT_RED = (72, 60, 240)       # #f03c48 recording
ACCENT_RED_SOFT = (46, 32, 78)
ACCENT_AMBER = (36, 178, 252)    # #fcb224 paused / warnings
ACCENT_GREEN = (110, 206, 80)    # #50ce6e ready

FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_BOLD = cv2.FONT_HERSHEY_DUPLEX

# --- Layout --------------------------------------------------------------------------------
MARGIN = 16
STACK_BELOW_WIDTH = 980          # Narrower windows stack the preview above the panel
MIN_CANVAS_W = 560               # Smaller windows show this canvas scaled down evenly
MIN_CANVAS_H = 600
CONTENT_MAX_H = 2400             # Tallest scrollable panel content
SCROLL_STEP = 60
FILES_PER_PAGE = 6
WINDOWS_PER_PAGE = 8
FILE_THUMB = (160, 90)
WINDOW_THUMB = (96, 54)

TABS = ["RECORDINGS", "CLIPS", "SOURCE", "SETTINGS"]
FPS_PRESETS = [30, 60, 120, 144, 240]
DURATION_PRESETS = [15, 30, 60, 300, 600, 1800, 3600]
SETTINGS_PAGES = ["GENERAL", "VIDEO", "AUDIO", "OVERLAY", "HOTKEYS"]
APPS_PER_PAGE = 8
RESOLUTION_LABELS = {"native": "Native", "2160p": "2160p", "1440p": "1440p", "1080p": "1080p",
                     "720p": "720p", "480p": "480p"}
QUALITY_LABELS = {"low": "Low", "medium": "Medium", "high": "High", "ultra": "Ultra", "custom": "Custom"}
DESKTOP_MODE_LABELS = {"all": "All Apps", "selected": "Only Selected Apps", "off": "Off"}
TRACK_LAYOUT_LABELS = {"mixed": "One Mixed Track", "separate": "Mixed + Track per Source"}
OVERLAY_STAT_LABELS = {"fps": "Live FPS", "source": "Capture Source", "elapsed": "Elapsed Time", "hotkeys": "Hotkeys"}
OVERLAY_POSITION_LABELS = {
    "top_left": "Top Left", "top_right": "Top Right",
    "bottom_left": "Bottom Left", "bottom_right": "Bottom Right", "custom": "Custom",
}
FOOTER_HOTKEY_NAMES = {"record_toggle": "Start / Stop", "pause": "Pause", "clip": "Clip", "lock_window": "Lock Window",
                       "mic_toggle": "Mic"}

Rect = Tuple[int, int, int, int]


# --- Drawing helpers -------------------------------------------------------------------------

def _lerp(a: Tuple[int, int, int], b: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _rounded_rect(img: np.ndarray, x: int, y: int, w: int, h: int, r: int, color, thickness: int = -1) -> None:
    """Rounded rectangle; thickness -1 fills it."""
    if w <= 0 or h <= 0:
        return
    r = max(0, min(r, w // 2, h // 2))
    if r == 0:
        cv2.rectangle(img, (x, y), (x + w, y + h), color, thickness)
        return
    if thickness < 0:
        cv2.rectangle(img, (x + r, y), (x + w - r, y + h), color, -1)
        cv2.rectangle(img, (x, y + r), (x + w, y + h - r), color, -1)
        for cx, cy in ((x + r, y + r), (x + w - r, y + r), (x + r, y + h - r), (x + w - r, y + h - r)):
            cv2.circle(img, (cx, cy), r, color, -1, cv2.LINE_AA)
        return
    t = thickness
    cv2.line(img, (x + r, y), (x + w - r, y), color, t, cv2.LINE_AA)
    cv2.line(img, (x + r, y + h), (x + w - r, y + h), color, t, cv2.LINE_AA)
    cv2.line(img, (x, y + r), (x, y + h - r), color, t, cv2.LINE_AA)
    cv2.line(img, (x + w, y + r), (x + w, y + h - r), color, t, cv2.LINE_AA)
    cv2.ellipse(img, (x + r, y + r), (r, r), 180, 0, 90, color, t, cv2.LINE_AA)
    cv2.ellipse(img, (x + w - r, y + r), (r, r), 270, 0, 90, color, t, cv2.LINE_AA)
    cv2.ellipse(img, (x + w - r, y + h - r), (r, r), 0, 0, 90, color, t, cv2.LINE_AA)
    cv2.ellipse(img, (x + r, y + h - r), (r, r), 90, 0, 90, color, t, cv2.LINE_AA)


def _shadow(img: np.ndarray, x: int, y: int, w: int, h: int, r: int, base, depth: int = 6) -> None:
    """Soft drop shadow: a few darker rounded rectangles offset downwards."""
    for i in range(depth, 0, -1):
        t = 0.55 * (depth - i + 1) / depth
        _rounded_rect(img, x - i + 1, y - i + 3, w + 2 * i - 2, h + 2 * i - 2, r + i, _lerp(base, SHADOW, t))


def _glow(img: np.ndarray, x: int, y: int, w: int, h: int, r: int, base, color, layers: int = 4) -> None:
    """Faint coloured halo used to make the live preview stand out."""
    for i in range(layers, 0, -1):
        _rounded_rect(img, x - i * 2, y - i * 2, w + i * 4, h + i * 4, r + i * 2,
                      _lerp(base, color, 0.07 * (layers - i + 1)))


def _text_size(text: str, scale: float, font=FONT, thickness: int = 1) -> Tuple[int, int]:
    return cv2.getTextSize(text, font, scale, thickness)[0]


def _fit(text: str, max_w: int, scale: float, font=FONT, thickness: int = 1) -> str:
    """Shortens text with "..." so it fits max_w pixels."""
    if max_w <= 0:
        return ""
    if _text_size(text, scale, font, thickness)[0] <= max_w:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _text_size(text[:mid] + "...", scale, font, thickness)[0] <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "..." if lo > 0 else ""


def _wrap(text: str, max_w: int, scale: float, font=FONT) -> List[str]:
    """Breaks text into lines that fit max_w pixels."""
    lines: List[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and _text_size(candidate, scale, font)[0] > max_w:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return [_fit(line, max_w, scale, font) for line in lines]


def _put(img: np.ndarray, text: str, x: int, y: int, scale: float, color, font=FONT, thickness: int = 1) -> None:
    cv2.putText(img, text, (int(x), int(y)), font, scale, color, thickness, cv2.LINE_AA)


def _icon(img: np.ndarray, kind: str, cx: int, cy: int, color) -> None:
    """Small vector icons drawn with primitives (Hershey fonts have no symbols)."""
    if kind == "dot":
        cv2.circle(img, (cx, cy), 6, color, -1, cv2.LINE_AA)
    elif kind == "stop":
        cv2.rectangle(img, (cx - 5, cy - 5), (cx + 5, cy + 5), color, -1)
    elif kind == "pause":
        cv2.rectangle(img, (cx - 5, cy - 6), (cx - 2, cy + 6), color, -1)
        cv2.rectangle(img, (cx + 2, cy - 6), (cx + 5, cy + 6), color, -1)
    elif kind == "play":
        points = np.array([[cx - 4, cy - 6], [cx - 4, cy + 6], [cx + 6, cy]], np.int32)
        cv2.fillPoly(img, [points], color, cv2.LINE_AA)
    elif kind == "clip":
        cv2.rectangle(img, (cx - 7, cy - 5), (cx + 7, cy + 5), color, 1, cv2.LINE_AA)
        points = np.array([[cx - 2, cy - 3], [cx - 2, cy + 3], [cx + 3, cy]], np.int32)
        cv2.fillPoly(img, [points], color, cv2.LINE_AA)
    elif kind == "menu":
        for dy in (-5, 0, 5):
            cv2.line(img, (cx - 7, cy + dy), (cx + 7, cy + dy), color, 2, cv2.LINE_AA)
    elif kind == "monitor":
        cv2.rectangle(img, (cx - 8, cy - 6), (cx + 8, cy + 4), color, 1, cv2.LINE_AA)
        cv2.line(img, (cx - 4, cy + 7), (cx + 4, cy + 7), color, 1, cv2.LINE_AA)
    elif kind == "x":
        cv2.line(img, (cx - 5, cy - 5), (cx + 5, cy + 5), color, 2, cv2.LINE_AA)
        cv2.line(img, (cx - 5, cy + 5), (cx + 5, cy - 5), color, 2, cv2.LINE_AA)


def _caret(img: np.ndarray, cx: int, cy: int, color, up: bool = False) -> None:
    if up:
        points = np.array([[cx - 5, cy + 3], [cx + 5, cy + 3], [cx, cy - 3]], np.int32)
    else:
        points = np.array([[cx - 5, cy - 3], [cx + 5, cy - 3], [cx, cy + 3]], np.int32)
    cv2.fillPoly(img, [points], color, cv2.LINE_AA)


def _flow(widths: List[int], width: int, gap: int, row_h: int, row_gap: int) -> Tuple[List[Rect], int]:
    """
    Places items left to right, wrapping onto new rows, and stretches each row to the
    full width. Returns rects relative to (0, 0) and the total height.
    """
    rows: List[List[int]] = []
    current: List[int] = []
    used = 0
    for w in widths:
        w = min(w, width)
        needed = w if not current else used + gap + w
        if current and needed > width:
            rows.append(current)
            current, used = [w], w
        else:
            current.append(w)
            used = needed
    if current:
        rows.append(current)

    rects: List[Rect] = []
    y = 0
    for row in rows:
        extra = width - (sum(row) + gap * (len(row) - 1))
        share = extra // len(row)
        x = 0
        for i, w in enumerate(row):
            w = w + share + (extra - share * len(row) if i == len(row) - 1 else 0)
            rects.append((x, y, w, row_h))
            x += w + gap
        y += row_h + row_gap
    return rects, (y - row_gap if rows else 0)


def _grid(count: int, width: int, min_w: int, gap: int, row_h: int, row_gap: int) -> Tuple[List[Rect], int]:
    """Equal-width cells, as many columns as fit."""
    columns = max(1, min(count, (width + gap) // (min_w + gap)))
    cell_w = (width - gap * (columns - 1)) // columns
    rects = [((i % columns) * (cell_w + gap), (i // columns) * (row_h + row_gap), cell_w, row_h) for i in range(count)]
    rows = math.ceil(count / columns) if count else 0
    return rects, (rows * (row_h + row_gap) - row_gap if rows else 0)


def _inside(rect: Optional[Rect], px: int, py: int) -> bool:
    if rect is None:
        return False
    x, y, w, h = rect
    return x <= px <= x + w and y <= py <= y + h


def _wheel_delta(flags: int) -> int:
    """
    Wheel movement from an OpenCV mouse event: positive = scroll up. OpenCV packs it in the
    high 16 bits of flags (cv2.getMouseWheelDelta isn't exposed in every Python build).
    """
    if flags > 0x7FFFFFFF:  # Delivered as unsigned 32-bit on some builds
        flags -= 1 << 32
    return flags >> 16  # Arithmetic shift keeps the sign


def _format_duration(seconds: float) -> str:
    """Exact length, e.g. 0:30.00 or 1:02:05.50."""
    total = max(0.0, seconds)
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    secs = total - hours * 3600 - minutes * 60
    if hours:
        return f"{hours}:{minutes:02d}:{secs:05.2f}"
    return f"{minutes}:{secs:05.2f}"


class ClickableArea:
    """A clickable rectangle in canvas coordinates, optionally clipped to a viewport."""

    __slots__ = ("x", "y", "w", "h", "action_id", "data", "clip")

    def __init__(self, rect: Rect, action_id: str, data: Optional[dict] = None, clip: Optional[Rect] = None):
        self.x, self.y, self.w, self.h = rect
        self.action_id = action_id
        self.data = data or {}
        self.clip = clip

    @property
    def key(self) -> Tuple[str, int, int]:
        return (self.action_id, self.x, self.y)

    def contains(self, px: int, py: int) -> bool:
        if self.clip is not None and not _inside(self.clip, px, py):
            return False
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h


class StudioDashboard:
    """
    Studio UI Dashboard. Operates in IDLE, RECORDING, and PAUSED states.
    The app reads the request_* flags each loop and sets the display attributes.
    """

    def __init__(
        self,
        recordings_dir: Path,
        clips_dir: Path,
        initial_fps: int = 30,
        initial_buffer_seconds: int = 30,
        burn_fps_initial: bool = False,
        show_fps_initial: bool = True,
        hotkeys=None,
    ):
        self.recordings_dir = Path(recordings_dir)
        self.clips_dir = Path(clips_dir)
        self.target_fps = initial_fps
        self.pending_fps = initial_fps
        self.buffer_seconds = initial_buffer_seconds
        self.pending_buffer_seconds = initial_buffer_seconds
        self.burn_fps_on_video = burn_fps_initial
        self.show_fps_on_preview = show_fps_initial
        self.hotkeys = hotkeys

        # Application state: "IDLE", "RECORDING", "PAUSED"
        self.state = "IDLE"
        self.replay_tier = "ram"  # "ram", "disk" or "off"; set by the app
        self.source_label = ""
        self.source_key: Optional[tuple] = None
        self.source_hwnd: Optional[int] = None
        self.monitor_device: Optional[str] = None
        self.source_minimized = False

        # Video output (set by the app) and the choices waiting for APPLY
        self.native_size: Tuple[int, int] = (1920, 1080)
        self.canvas_size: Tuple[int, int] = (1920, 1080)
        self.pending_resolution = config.OUTPUT_RESOLUTION
        self.pending_quality = config.VIDEO_QUALITY
        self.pending_bitrate = config.VIDEO_BITRATE_KBPS

        # Audio routing (the app's AudioRouter, set by the app)
        self.audio = None
        self.audio_apps_page = 0
        self._audio_versions: Tuple[int, int] = (-1, -1)

        # Navigation
        self.active_tab = "RECORDINGS"
        self.settings_page = "GENERAL"
        self.recordings_page = 0
        self.clips_page = 0
        self.source_page = 0
        self.menu_open = False
        self.scroll: Dict[str, int] = {tab: 0 for tab in TABS}
        self._content_h: Dict[str, int] = {tab: 0 for tab in TABS}

        # Files and thumbnails
        self.recordings_files: List[Dict] = []
        self.clips_files: List[Dict] = []
        self.last_files_scan = 0.0
        self.media = MediaInfoCache(FILE_THUMB, WINDOW_THUMB)
        self._media_version = 0

        # Capture source lists (refreshed when the SOURCE tab opens)
        self.displays: List[Dict] = []
        self.windows: List[Tuple[int, str]] = []
        self.minimized_windows: set = set()
        self.window_capture_available = window_capture_available()

        # Hotkey rebinding
        self.rebinding_action: Optional[str] = None
        self._pending_rebind: Optional[Tuple[str, Optional[str]]] = None

        # Mouse
        self.mouse_x = -1
        self.mouse_y = -1
        self.hovered_key: Optional[tuple] = None
        self.clickables: List[ClickableArea] = []

        # Drawing context: translation and clip for the scrolled panel content
        self._ox = 0
        self._oy = 0
        self._clip: Optional[Rect] = None

        # Overlay screen map (geometry of the last drawn map) and an active drag
        self._map: Optional[Dict] = None
        self._drag: Optional[Dict] = None

        # Action requests for main loop
        self.request_start_recording = False
        self.request_stop_recording = False
        self.request_pause_toggle = False
        self.request_clip = False
        self.request_exit_app = False
        self.request_fps_change: Optional[int] = None
        self.request_buffer_change: Optional[int] = None
        self.request_replay_toggle = False
        self.request_source: Optional[dict] = None
        self.request_overlay_update = False
        self.request_video_change: Optional[dict] = None
        self.request_audio_update = False
        self.request_mic_toggle = False

        # Toast / status messages
        record_key = display_name(self.hotkeys.binding("record_toggle")) if self.hotkeys else "F9"
        self.toast_text: str = f"Ready. Click START RECORDING or press {record_key} to begin."
        self.toast_color: Tuple[int, int, int] = TEXT_GRAY

        # Delete confirmation tracking
        self.delete_confirm_path: Optional[Path] = None
        self.delete_confirm_time: float = 0.0

        # Canvas (resized to the window by set_window_size)
        self.canvas_w = config.WINDOW_WIDTH
        self.canvas_h = config.WINDOW_HEIGHT
        self.base_canvas = np.full((self.canvas_h, self.canvas_w, 3), BG, dtype=np.uint8)
        self.dirty = True
        self._relayout = False
        self._last_state = "IDLE"
        self._layout: Dict = {}
        self._preview_fit_key: Optional[tuple] = None
        self._preview_fit: Rect = (0, 0, 1, 1)
        self._preview_src: Optional[Tuple[int, int]] = None  # Size of the last previewed frame
        self._baked_glow = None  # (fit, state) of the glow drawn into base_canvas

        self.refresh_file_lists()

    # --- Window size ------------------------------------------------------------------

    def set_window_size(self, width: int, height: int) -> None:
        """
        Matches the canvas to the window's image area so it is shown 1:1, never stretched.
        Below the minimum layout size the canvas keeps the window's aspect ratio, so the
        window shrinks it evenly. Mouse positions arrive in canvas pixels either way.
        """
        if width <= 0 or height <= 0:
            return  # Minimized
        scale = max(1.0, MIN_CANVAS_W / width, MIN_CANVAS_H / height)
        new_size = (int(round(width * scale)), int(round(height * scale)))
        if new_size != (self.canvas_w, self.canvas_h):
            self.canvas_w, self.canvas_h = new_size
            self.menu_open = False
            self.dirty = True

    # --- Helpers ----------------------------------------------------------------------

    @staticmethod
    def duration_label(seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}s"
        if seconds % 60 == 0:
            return f"{seconds // 60} min"
        return f"{seconds // 60}m {seconds % 60}s"

    def set_toast(self, text: str, color: Tuple[int, int, int] = TEXT_GRAY) -> None:
        """Displays a notification toast in the UI."""
        self.toast_text = text
        self.toast_color = color

    def _hotkey_summary(self, separator: str) -> str:
        if self.hotkeys is None:
            return ""
        parts = [
            f"[{display_name(combo)}] {FOOTER_HOTKEY_NAMES.get(action, action)}"
            for action, combo in self.hotkeys.bindings().items() if combo
        ]
        return separator.join(parts + ["[ESC] Exit"])

    def _hotkey_line_count(self) -> int:
        """Lines the overlay uses for hotkeys (two per line)."""
        if self.hotkeys is None:
            return 0
        bound = sum(1 for combo in self.hotkeys.bindings().values() if combo)
        return math.ceil(bound / 2)

    def refresh_file_lists(self) -> None:
        """Scans the Recordings and Clips directories and populates metadata."""
        self.recordings_files = self._scan_directory(self.recordings_dir)
        self.clips_files = self._scan_directory(self.clips_dir)
        self.media.prune_files(
            (str(f["path"]), f["mtime"], f["size"]) for f in self.recordings_files + self.clips_files
        )
        self.last_files_scan = time.time()
        self.dirty = True

    def refresh_sources(self) -> None:
        """Re-enumerates displays and capturable windows, and retakes window stills."""
        try:
            self.displays = list_displays()
        except Exception as e:
            self.displays = []
            self.set_toast(f"Could not list displays: {e}", ACCENT_RED)
        self.windows = win32_api.list_windows() if self.window_capture_available else []
        self.minimized_windows = {hwnd for hwnd, _ in self.windows if win32_api.is_minimized(hwnd)}
        self.media.forget_windows()
        self.source_page = 0
        self.dirty = True

    def _scan_directory(self, directory: Path) -> List[Dict]:
        """Reads video files from a directory, sorted newest first."""
        if not directory.exists():
            return []

        entries = []
        for file in directory.glob("*.*"):
            if file.suffix.lower() in (".mp4", ".avi", ".mkv", ".mov"):
                try:
                    stat = file.stat()
                    entries.append({
                        "path": file,
                        "name": file.name,
                        "size": stat.st_size,
                        "size_mb": stat.st_size / (1024 * 1024),
                        "time_str": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                        "mtime": stat.st_mtime,
                    })
                except OSError:
                    continue

        entries.sort(key=lambda x: x["mtime"], reverse=True)
        return entries

    # --- Input --------------------------------------------------------------------------

    def _hit(self, x: int, y: int) -> Optional[ClickableArea]:
        # Last drawn is on top (e.g. the menu dropdown over the content).
        for item in reversed(self.clickables):
            if item.contains(x, y):
                return item
        return None

    def on_mouse(self, event: int, x: int, y: int, flags: int, param: any) -> None:
        """Handles OpenCV mouse events."""
        if event == cv2.EVENT_MOUSEWHEEL:
            # Uses the last pointer position: wheel coordinates aren't reliable on every build.
            self._on_wheel(_wheel_delta(flags))
            return

        self.mouse_x, self.mouse_y = x, y

        if self._drag is not None:
            released = event == cv2.EVENT_LBUTTONUP or (
                event == cv2.EVENT_MOUSEMOVE and not (flags & cv2.EVENT_FLAG_LBUTTON)
            )
            self._update_drag(x, y)
            if released:
                self._finish_drag()
            return

        item = self._hit(x, y)
        key = item.key if item is not None else None
        if key != self.hovered_key:
            self.hovered_key = key
            self.dirty = True

        if event != cv2.EVENT_LBUTTONDOWN:
            return
        if self.menu_open and (item is None or not item.action_id.startswith(("tab_", "menu_toggle"))):
            # A click outside the open menu just closes it.
            self.menu_open = False
            self.dirty = True
            return
        if item is None:
            return
        if item.action_id == "overlay_map":
            self._start_drag(x, y)
            return
        self._handle_action(item.action_id, item.data)
        self.dirty = True

    def _on_wheel(self, delta: int) -> None:
        viewport = self._layout.get("viewport")
        if delta == 0 or self.menu_open or not _inside(viewport, self.mouse_x, self.mouse_y):
            return
        tab = self.active_tab
        max_scroll = max(0, self._content_h.get(tab, 0) - viewport[3])
        step = -SCROLL_STEP if delta > 0 else SCROLL_STEP
        new_scroll = max(0, min(max_scroll, self.scroll[tab] + step))
        if new_scroll != self.scroll[tab]:
            self.scroll[tab] = new_scroll
            self.hovered_key = None
            self.dirty = True

    def _handle_action(self, action_id: str, data: dict) -> None:
        """Dispatches button clicks."""
        # Header actions
        if action_id == "start_recording":
            self.request_start_recording = True

        elif action_id == "stop_recording":
            self.request_stop_recording = True

        elif action_id == "toggle_pause":
            self.request_pause_toggle = True

        elif action_id == "trigger_clip":
            self.request_clip = True

        elif action_id == "exit_app":
            self.request_exit_app = True

        # Navigation
        elif action_id == "menu_toggle":
            self.menu_open = not self.menu_open

        elif action_id.startswith("tab_"):
            self.active_tab = action_id[4:].upper()
            self.menu_open = False
            if self.active_tab == "SOURCE":
                self.refresh_sources()

        elif action_id.startswith("settings_page_"):
            self.settings_page = action_id[len("settings_page_"):].upper()
            self.scroll["SETTINGS"] = 0
            if self.settings_page == "AUDIO" and self.audio is not None:
                self.audio.request_catalog()
            if self.settings_page == "VIDEO":
                self.pending_resolution = config.OUTPUT_RESOLUTION
                self.pending_quality = config.VIDEO_QUALITY
                self.pending_bitrate = config.VIDEO_BITRATE_KBPS

        # Video settings (applied together with APPLY: a change starts a new recording segment)
        elif action_id.startswith("res_"):
            self.pending_resolution = action_id[len("res_"):]

        elif action_id.startswith("quality_"):
            quality = action_id[len("quality_"):]
            if quality == "custom" and self.pending_quality != "custom":
                self.pending_bitrate = self._pending_bitrate_kbps()  # Start from the current preset
            self.pending_quality = quality

        elif action_id.startswith("bitrate_step_"):
            start = self._pending_bitrate_kbps()
            self.pending_bitrate = config.clamp_bitrate(start + int(action_id[len("bitrate_step_"):]))
            self.pending_quality = "custom"

        elif action_id == "video_apply":
            if self._video_pending():
                self.request_video_change = {"resolution": self.pending_resolution,
                                             "quality": self.pending_quality, "bitrate": self.pending_bitrate}

        # Audio routing (saved here, applied by the app's audio router)
        elif action_id == "mic_toggle":
            self.request_mic_toggle = True

        elif action_id.startswith("desktop_mode_"):
            config.AUDIO_DESKTOP_MODE = action_id[len("desktop_mode_"):]
            self._audio_changed()
            self.set_toast(f"Desktop audio: {DESKTOP_MODE_LABELS[config.AUDIO_DESKTOP_MODE]}", TEXT_WHITE)

        elif action_id.startswith("track_layout_"):
            config.AUDIO_TRACK_LAYOUT = action_id[len("track_layout_"):]
            config.save_user_settings()
            self.set_toast(f"Audio tracks: {TRACK_LAYOUT_LABELS[config.AUDIO_TRACK_LAYOUT]} "
                           "(applies to files saved from now on)", TEXT_WHITE)

        elif action_id == "audio_app_rule":
            self._toggle_app_rule(data["exe"])

        elif action_id == "refresh_audio_apps":
            if self.audio is not None:
                self.audio.request_catalog()
            self.set_toast("Refreshing the application list...", TEXT_WHITE)

        # General settings: framerate (applied with APPLY so each click doesn't restart the segment)
        elif action_id.startswith("fps_step_"):
            self.pending_fps = config.clamp_fps(self.pending_fps + int(action_id[len("fps_step_"):]))

        elif action_id.startswith("fps_preset_"):
            self.pending_fps = config.clamp_fps(int(action_id[len("fps_preset_"):]))

        elif action_id == "fps_apply":
            if self.pending_fps != self.target_fps:
                self.request_fps_change = self.pending_fps

        # General settings: replay duration
        elif action_id.startswith("buf_step_"):
            self.pending_buffer_seconds = config.clamp_clip_duration(
                self.pending_buffer_seconds + int(action_id[len("buf_step_"):])
            )

        elif action_id.startswith("buf_preset_"):
            self.pending_buffer_seconds = config.clamp_clip_duration(int(action_id[len("buf_preset_"):]))

        elif action_id == "buf_apply":
            if self.pending_buffer_seconds != self.buffer_seconds:
                self.request_buffer_change = self.pending_buffer_seconds

        elif action_id == "toggle_replay":
            self.request_replay_toggle = True

        elif action_id == "toggle_burn_fps":
            self.burn_fps_on_video = not self.burn_fps_on_video
            config.BURN_FPS_ON_RECORDING = self.burn_fps_on_video
            config.save_user_settings()
            status = "ON" if self.burn_fps_on_video else "OFF"
            self.set_toast(f"Burn FPS watermark on recorded video: {status}", TEXT_WHITE)

        elif action_id == "toggle_show_fps":
            self.show_fps_on_preview = not self.show_fps_on_preview
            config.SHOW_FPS_ON_PREVIEW = self.show_fps_on_preview
            config.save_user_settings()
            status = "ON" if self.show_fps_on_preview else "OFF"
            self.set_toast(f"Show FPS on preview: {status}", TEXT_WHITE)

        # Overlay settings (saved here, applied by the app)
        elif action_id == "overlay_toggle":
            config.OVERLAY_ENABLED = not config.OVERLAY_ENABLED
            self._overlay_changed()
            self.set_toast(f"Live overlay: {'ON' if config.OVERLAY_ENABLED else 'OFF'}", TEXT_WHITE)

        elif action_id.startswith("overlay_stat_"):
            key = action_id[len("overlay_stat_"):]
            config.OVERLAY_STATS[key] = not config.OVERLAY_STATS.get(key, False)
            self._overlay_changed()

        elif action_id.startswith("overlay_pos_"):
            config.OVERLAY_POSITION = action_id[len("overlay_pos_"):]
            self._overlay_changed()

        elif action_id.startswith("overlay_mon_"):
            config.OVERLAY_MONITOR = action_id[len("overlay_mon_"):]
            self._overlay_changed()

        # Hotkeys
        elif action_id.startswith("hk_rebind_"):
            self._begin_rebind(action_id[len("hk_rebind_"):])

        elif action_id.startswith("hk_clear_"):
            self._set_binding(action_id[len("hk_clear_"):], "")

        # Capture sources
        elif action_id == "refresh_sources":
            self.refresh_sources()
            self.set_toast(f"Found {len(self.displays)} displays and {len(self.windows)} windows.", TEXT_WHITE)

        elif action_id.startswith("src_display_"):
            self.request_source = {"kind": "display", "display": data["display"]}

        elif action_id.startswith("src_window_"):
            self.request_source = {"kind": "window", "hwnd": data["hwnd"], "title": data["title"]}
            self.minimized_windows.discard(data["hwnd"])  # Restored on selection

        # File Operations
        elif action_id == "refresh_files":
            self.refresh_file_lists()
            self.set_toast("Refreshed file list.", TEXT_WHITE)

        elif action_id == "open_current_folder":
            target_dir = self.recordings_dir if self.active_tab == "RECORDINGS" else self.clips_dir
            try:
                os.startfile(str(target_dir.resolve()))
                self.set_toast(f"Opened folder: {target_dir.name}", TEXT_WHITE)
            except Exception as e:
                self.set_toast(f"Failed to open folder: {e}", ACCENT_RED)

        elif action_id == "play_file":
            filepath = data.get("path")
            if filepath and filepath.exists():
                try:
                    os.startfile(str(filepath.resolve()))
                    self.set_toast(f"Playing: {filepath.name}", ACCENT_GREEN)
                except Exception as e:
                    self.set_toast(f"Error launching player: {e}", ACCENT_RED)

        elif action_id == "show_file":
            filepath = data.get("path")
            if filepath and filepath.exists():
                try:
                    subprocess.Popen(["explorer", f"/select,{str(filepath.resolve())}"])
                    self.set_toast(f"Highlighted in Explorer: {filepath.name}", TEXT_WHITE)
                except Exception as e:
                    self.set_toast(f"Explorer error: {e}", ACCENT_RED)

        elif action_id == "delete_file":
            filepath: Optional[Path] = data.get("path")
            if filepath and filepath.exists():
                now = time.time()
                if self.delete_confirm_path == filepath and (now - self.delete_confirm_time) < 4.0:
                    try:
                        filepath.unlink(missing_ok=True)
                        self.set_toast(f"Deleted file: {filepath.name}", ACCENT_AMBER)
                        self.delete_confirm_path = None
                        self.refresh_file_lists()
                    except Exception as e:
                        self.set_toast(f"Delete failed: {e}", ACCENT_RED)
                else:
                    self.delete_confirm_path = filepath
                    self.delete_confirm_time = now
                    self.set_toast(f"Click again to confirm deleting '{filepath.name}'", ACCENT_AMBER)

        # Pagination
        elif action_id in ("prev_page", "next_page"):
            step = -1 if action_id == "prev_page" else 1
            if self.active_tab == "RECORDINGS":
                pages = max(1, math.ceil(len(self.recordings_files) / FILES_PER_PAGE))
                self.recordings_page = max(0, min(pages - 1, self.recordings_page + step))
            elif self.active_tab == "CLIPS":
                pages = max(1, math.ceil(len(self.clips_files) / FILES_PER_PAGE))
                self.clips_page = max(0, min(pages - 1, self.clips_page + step))
            elif self.active_tab == "SOURCE":
                pages = max(1, math.ceil(len(self.windows) / WINDOWS_PER_PAGE))
                self.source_page = max(0, min(pages - 1, self.source_page + step))
            elif self.active_tab == "SETTINGS":
                catalog = self.audio.catalog if self.audio is not None else []
                pages = max(1, math.ceil(len(catalog) / APPS_PER_PAGE))
                self.audio_apps_page = max(0, min(pages - 1, self.audio_apps_page + step))
                return  # Keep the scroll position: the list sits below the audio controls
            self.scroll[self.active_tab] = 0

        self.dirty = True

    def _overlay_changed(self) -> None:
        config.save_user_settings()
        self.request_overlay_update = True

    # --- Video and audio settings ------------------------------------------------------------

    def _pending_canvas(self) -> Tuple[int, int]:
        return config.output_canvas_size(self.native_size, self.pending_resolution)

    def _pending_bitrate_kbps(self) -> int:
        return target_bitrate_kbps(self._pending_canvas(), self.target_fps, self.pending_quality, self.pending_bitrate)

    def _video_pending(self) -> bool:
        if self.pending_resolution != config.OUTPUT_RESOLUTION or self.pending_quality != config.VIDEO_QUALITY:
            return True
        return self.pending_quality == "custom" and self.pending_bitrate != config.VIDEO_BITRATE_KBPS

    def quality_label(self) -> str:
        kbps = target_bitrate_kbps(self.canvas_size, self.target_fps)
        return f"{QUALITY_LABELS.get(config.VIDEO_QUALITY, 'High')} quality, {kbps / 1000:.1f} Mbps"

    def _audio_changed(self) -> None:
        config.save_user_settings()
        self.request_audio_update = True

    def _toggle_app_rule(self, exe: str) -> None:
        """Blocks/unblocks an app (All Apps mode) or includes/excludes it (Only Selected Apps mode)."""
        if config.AUDIO_DESKTOP_MODE == "selected":
            apps = set(config.AUDIO_INCLUDED_APPS)
            apps.symmetric_difference_update({exe})
            config.AUDIO_INCLUDED_APPS = sorted(apps)
            verb = "included" if exe in apps else "not included"
        elif config.AUDIO_DESKTOP_MODE == "all":
            apps = set(config.AUDIO_BLOCKED_APPS)
            apps.symmetric_difference_update({exe})
            config.AUDIO_BLOCKED_APPS = sorted(apps)
            verb = "blocked" if exe in apps else "recorded"
        else:
            return
        self._audio_changed()
        self.set_toast(f"{exe}: {verb}", TEXT_WHITE)

    # --- Overlay drag ---------------------------------------------------------------------

    def _start_drag(self, x: int, y: int) -> None:
        geometry = self._map
        if geometry is None:
            return
        mock_x, mock_y, mock_w, mock_h = geometry["mock"]
        if _inside(geometry["mock"], x, y):
            grab = (x - mock_x, y - mock_y)
        else:
            grab = (mock_w / 2, mock_h / 2)  # Clicking the map moves the box there
        self._drag = {"grab": grab, "pos": geometry["pos"]}
        self._update_drag(x, y)

    def _update_drag(self, x: int, y: int) -> None:
        geometry, drag = self._map, self._drag
        if geometry is None or drag is None:
            return
        origin_x, origin_y = geometry["origin"]
        scale = geometry["scale"]
        width, height = geometry["size"]
        monitor_w, monitor_h = geometry["monitor_size"]
        px = (x - drag["grab"][0] - origin_x) / scale
        py = (y - drag["grab"][1] - origin_y) / scale
        position = (
            int(round(max(0, min(monitor_w - width, px)))),
            int(round(max(0, min(monitor_h - height, py)))),
        )
        if position != drag["pos"]:
            drag["pos"] = position
            self.dirty = True

    def _finish_drag(self) -> None:
        x, y = self._drag["pos"]
        self._drag = None
        config.OVERLAY_POSITION = "custom"
        config.OVERLAY_X, config.OVERLAY_Y = x, y
        self._overlay_changed()
        self.set_toast(f"Overlay position saved: X {x}, Y {y}", TEXT_WHITE)
        self.dirty = True

    # --- Hotkey rebinding -------------------------------------------------------------------

    def _begin_rebind(self, action: str) -> None:
        if self.hotkeys is None or self.hotkeys.capturing:
            return

        def _captured(combo: Optional[str]) -> None:
            # Runs on the capture thread; applied on the main thread in render().
            self._pending_rebind = (action, combo)

        if self.hotkeys.start_capture(_captured):
            self.rebinding_action = action
            label = config.HOTKEY_LABELS.get(action, action)
            self.set_toast(f"Press the new keys for '{label}' (Esc cancels).", ACCENT_AMBER)

    def _apply_pending_rebind(self) -> None:
        pending = self._pending_rebind
        if pending is None:
            return
        self._pending_rebind = None
        self.rebinding_action = None
        self.dirty = True
        action, combo = pending
        if combo is None:
            self.set_toast("Hotkey change cancelled.", TEXT_GRAY)
            return
        self._set_binding(action, combo)

    def _set_binding(self, action: str, combo: str) -> None:
        if self.hotkeys is None:
            return
        error = self.hotkeys.rebind(action, combo)
        if error:
            self.set_toast(error, ACCENT_RED)
            return
        config.HOTKEYS = self.hotkeys.bindings()
        config.save_user_settings()
        label = config.HOTKEY_LABELS.get(action, action)
        self.set_toast(f"{label}: {display_name(combo)}", TEXT_WHITE)
        self.dirty = True

    # --- Widgets ------------------------------------------------------------------------------

    def _register(self, rect: Rect, action_id: str, data: Optional[dict] = None) -> bool:
        """Adds a clickable area (in the current drawing context). Returns whether it is hovered."""
        x, y, w, h = rect
        area = ClickableArea((x + self._ox, y + self._oy, w, h), action_id, data, self._clip)
        self.clickables.append(area)
        return self._drag is None and self.hovered_key == area.key

    @staticmethod
    def _button_colors(style: str, active: bool, hover: bool):
        """(fill, border, text colour, border width) for a button style."""
        if style == "primary":
            return (ACCENT_HOVER if hover else ACCENT), None, TEXT_WHITE, 1
        if style == "record":
            return (_lerp(ACCENT_RED, TEXT_WHITE, 0.15) if hover else ACCENT_RED), None, TEXT_WHITE, 1
        if style == "danger":
            return (_lerp(ACCENT_RED_SOFT, ACCENT_RED, 0.3) if hover else ACCENT_RED_SOFT), ACCENT_RED, TEXT_WHITE, 1
        if style == "warn":
            fill = _lerp(SURFACE, ACCENT_AMBER, 0.38 if hover else 0.24)
            return fill, ACCENT_AMBER, TEXT_WHITE, 1
        if style in ("tab", "menuitem"):
            if active:
                return (ACCENT if style == "tab" else ACCENT_SOFT), None, TEXT_WHITE, 1
            return (CARD_HOVER if hover else None), None, (TEXT_WHITE if hover else TEXT_LIGHT), 1
        # default / option
        if active:
            return (_lerp(ACCENT_SOFT, ACCENT, 0.2) if hover else ACCENT_SOFT), ACCENT, TEXT_WHITE, 2
        if hover:
            return CARD_HOVER, BORDER_STRONG, TEXT_WHITE, 1
        return CARD, BORDER, TEXT_LIGHT, 1

    def _draw_button(
        self,
        img: np.ndarray,
        rect: Rect,
        text: str,
        action_id: str,
        data: Optional[dict] = None,
        active: bool = False,
        style: str = "default",
        scale: float = 0.42,
        icon: Optional[str] = None,
        align: str = "center",
        caret: Optional[str] = None,
    ) -> None:
        x, y, w, h = rect
        hover = self._register(rect, action_id, data)
        fill, border, fg, border_w = self._button_colors(style, active, hover)
        if fill is not None:
            _rounded_rect(img, x, y, w, h, 6, fill)
        if border is not None:
            _rounded_rect(img, x, y, w, h, 6, border, border_w)
        if style == "menuitem" and active:
            _rounded_rect(img, x, y + 6, 4, h - 12, 2, ACCENT)

        icon_w = 20 if icon else 0
        caret_w = 18 if caret else 0
        label = _fit(text, w - 20 - icon_w - caret_w, scale)
        tw, th = _text_size(label, scale)
        group = icon_w + tw
        gx = x + 14 if align == "left" else x + max(8, (w - caret_w - group) // 2)
        if icon:
            _icon(img, icon, gx + 7, y + h // 2, ACCENT_RED if icon == "dot" and style != "record" else fg)
        _put(img, label, gx + icon_w, y + (h + th) // 2, scale, fg)
        if caret:
            _caret(img, x + w - 16, y + h // 2, fg, up=(caret == "up"))

    def _spec_width(self, spec: Dict, scale: float, padding: int = 30) -> int:
        width = _text_size(spec["label"], scale)[0] + padding + (20 if spec.get("icon") else 0)
        return max(spec.get("min_w", 0), width)

    def _buttons_flow(self, img: np.ndarray, x: int, y: int, width: int, specs: List[Dict],
                      h: int = 32, gap: int = 8, scale: float = 0.42) -> int:
        """Buttons sized to their labels, wrapping onto new rows. Returns the y below them."""
        rects, total_h = _flow([self._spec_width(s, scale) for s in specs], width, gap, h, gap)
        for spec, (rx, ry, rw, rh) in zip(specs, rects):
            self._draw_button(img, (x + rx, y + ry, rw, rh), spec["label"], spec["action"], spec.get("data"),
                              spec.get("active", False), spec.get("style", "default"), scale, spec.get("icon"))
        return y + total_h

    def _buttons_grid(self, img: np.ndarray, x: int, y: int, width: int, specs: List[Dict], min_w: int,
                      h: int = 30, gap: int = 8, scale: float = 0.4) -> int:
        """Equal-width buttons in as many columns as fit. Returns the y below them."""
        rects, total_h = _grid(len(specs), width, min_w, gap, h, gap)
        for spec, (rx, ry, rw, rh) in zip(specs, rects):
            self._draw_button(img, (x + rx, y + ry, rw, rh), spec["label"], spec["action"], spec.get("data"),
                              spec.get("active", False), spec.get("style", "default"), scale, spec.get("icon"))
        return y + total_h

    def _draw_toggle(self, img: np.ndarray, x: int, y: int, w: int, label: str, on: bool, action_id: str,
                     h: int = 40) -> int:
        """A full-width row with a label and an on/off switch. Returns the y below it."""
        hover = self._register((x, y, w, h), action_id)
        _rounded_rect(img, x, y, w, h, 8, CARD_HOVER if hover else CARD)
        _rounded_rect(img, x, y, w, h, 8, ACCENT if on else (BORDER_STRONG if hover else BORDER), 2 if on else 1)
        track_w, track_h = 40, 20
        tx, ty = x + w - track_w - 14, y + (h - track_h) // 2
        _rounded_rect(img, tx, ty, track_w, track_h, track_h // 2, ACCENT if on else BORDER)
        knob_x = tx + track_w - track_h // 2 if on else tx + track_h // 2
        cv2.circle(img, (knob_x, ty + track_h // 2), track_h // 2 - 3, TEXT_WHITE, -1, cv2.LINE_AA)
        _put(img, _fit(label, w - track_w - 44, 0.42), x + 14, y + h // 2 + 5, 0.42, TEXT_WHITE if on else TEXT_LIGHT)
        return y + h

    @staticmethod
    def _draw_title(img: np.ndarray, x: int, y: int, width: int, title: str, subtitle: Optional[str] = None) -> int:
        """Section heading with an accent bar and an optional wrapped subtitle. Returns the y below."""
        _rounded_rect(img, x, y + 2, 3, 16, 1, ACCENT)
        _put(img, _fit(title, width - 12, 0.5, FONT_BOLD), x + 12, y + 16, 0.5, TEXT_WHITE, FONT_BOLD)
        y += 26
        if subtitle:
            for line in _wrap(subtitle, width, 0.38):
                _put(img, line, x, y + 12, 0.38, TEXT_GRAY)
                y += 17
            y += 6
        return y

    @staticmethod
    def _draw_notes(img: np.ndarray, x: int, y: int, width: int, notes: List[str]) -> int:
        for note in notes:
            for line in _wrap(note, width, 0.37):
                _put(img, line, x, y + 12, 0.37, TEXT_GRAY)
                y += 17
            y += 3
        return y

    @staticmethod
    def _draw_value_box(img: np.ndarray, rect: Rect, text: str) -> None:
        x, y, w, h = rect
        _rounded_rect(img, x, y, w, h, 6, SCREEN)
        _rounded_rect(img, x, y, w, h, 6, BORDER_STRONG, 1)
        label = _fit(text, w - 10, 0.48, FONT_BOLD)
        tw, th = _text_size(label, 0.48, FONT_BOLD)
        _put(img, label, x + (w - tw) // 2, y + (h + th) // 2, 0.48, TEXT_WHITE, FONT_BOLD)

    def _draw_thumbnail(self, img: np.ndarray, x: int, y: int, w: int, h: int, image: Optional[np.ndarray],
                        placeholder: str, emphasis=None) -> None:
        """A thumbnail well with a drop shadow and a light frame so it stands out."""
        _shadow(img, x, y, w, h, 4, SURFACE, depth=4)
        _rounded_rect(img, x, y, w, h, 4, SCREEN)
        if y + h > img.shape[0] or x + w > img.shape[1]:
            image = None  # Past the end of the scrollable content
        if image is not None:
            if image.shape[1] != w or image.shape[0] != h:
                image = cv2.resize(image, (w, h), interpolation=cv2.INTER_AREA)
            img[y:y + h, x:x + w] = image
        else:
            label = _fit(placeholder, w - 8, 0.36)
            tw, _ = _text_size(label, 0.36)
            _put(img, label, x + (w - tw) // 2, y + h // 2 + 4, 0.36, TEXT_DIM)
        cv2.rectangle(img, (x - 1, y - 1), (x + w, y + h), emphasis or BORDER_STRONG, 1, cv2.LINE_AA)

    @staticmethod
    def _pill(img: np.ndarray, x: int, y: int, text: str, fg, bg, scale: float = 0.4, border=None,
              align_right: bool = False) -> int:
        """Rounded label. Returns its width."""
        tw, th = _text_size(text, scale)
        w, h = tw + 18, th + 12
        if align_right:
            x -= w
        _rounded_rect(img, x, y, w, h, h // 2, bg)
        if border is not None:
            _rounded_rect(img, x, y, w, h, h // 2, border, 1)
        _put(img, text, x + 9, y + h - 6, scale, fg)
        return w

    # --- Layout ----------------------------------------------------------------------------------

    def _header_actions(self, state: str) -> List[Dict]:
        if state == "IDLE":
            return [
                {"label": "START RECORDING", "action": "start_recording", "style": "record", "icon": "dot", "min_w": 200},
                {"label": "EXIT", "action": "exit_app", "icon": "x", "min_w": 96},
            ]
        paused = state == "PAUSED"
        return [
            {"label": "STOP", "action": "stop_recording", "style": "danger", "icon": "stop", "min_w": 112},
            {"label": "RESUME" if paused else "PAUSE", "action": "toggle_pause",
             "style": "warn" if paused else "default", "icon": "play" if paused else "pause", "min_w": 124},
            {"label": f"CLIP {self.duration_label(self.buffer_seconds)}", "action": "trigger_clip",
             "style": "primary", "icon": "clip", "min_w": 140},
        ]

    def _tab_labels(self) -> List[Tuple[str, str]]:
        counts = {"RECORDINGS": len(self.recordings_files), "CLIPS": len(self.clips_files)}
        return [(tab, f"{tab} ({counts[tab]})" if tab in counts else tab) for tab in TABS]

    def _compute_layout(self, state: str) -> Dict:
        W, H, M = self.canvas_w, self.canvas_h, MARGIN
        layout: Dict = {"stacked": W < STACK_BELOW_WIDTH}

        # Header: brand, status chip, timer, FPS on the left; actions on the right, or
        # on their own wrapping row when the window is too narrow for one row.
        actions = self._header_actions(state)
        widths = [self._spec_width(a, 0.46, 34) for a in actions]
        actions_w = sum(widths) + 10 * (len(widths) - 1)
        brand_w, hud_w, row_h = 214, 104 + 14 + 120 + 16 + 150, 60
        if W >= brand_w + hud_w + actions_w + 4 * M:
            show_brand, two_rows = True, False
        elif W >= hud_w + actions_w + 3 * M:
            show_brand, two_rows = False, False
        else:
            show_brand, two_rows = W >= brand_w + hud_w + 3 * M, True

        x = M + (brand_w if show_brand else 0)
        layout["brand"] = show_brand
        layout["chip"] = (x, 16)
        layout["timer"] = (x + 118, 40)
        layout["fps"] = (x + 118 + 136, 37)
        layout["actions"] = []
        if two_rows:
            rects, total_h = _flow(widths, W - 2 * M, 10, 40, 8)
            layout["actions"] = [(M + rx, row_h + ry, rw, rh) for rx, ry, rw, rh in rects]
            header_h = row_h + total_h + 12
            layout["fps_w"] = W - M - layout["fps"][0]
        else:
            ax = W - M - actions_w
            for w in widths:
                layout["actions"].append((ax, 10, w, 40))
                ax += w + 10
            header_h = row_h
            layout["fps_w"] = W - 2 * M - actions_w - layout["fps"][0]
        layout["header_h"] = header_h

        if not layout["stacked"]:
            panel_w = max(420, min(580, int(W * 0.36)))
            panel_x = W - panel_w
            footer_h, gauge_h = 24, 30
            gauge_y = H - M - footer_h - gauge_h
            preview_y = header_h + M
            layout["preview"] = (M, preview_y, panel_x - 2 * M, max(60, gauge_y - 14 - preview_y))
            layout["gauge"] = (M, gauge_y, panel_x - 2 * M, gauge_h)
            layout["footer"] = (M + 4, H - M - 4, panel_x - 2 * M - 8)
            layout["panel"] = (panel_x, header_h, panel_w, H - header_h)
            inner_x, inner_w = panel_x + M, panel_w - 2 * M
            nav_y = header_h + M
            toast_h = 44
        else:
            available = W - 2 * M
            preview_h = max(100, min(int(available * 9 / 16), int((H - header_h) * 0.32)))
            preview_y = header_h + M
            gauge_y = preview_y + preview_h + 10
            layout["preview"] = (M, preview_y, available, preview_h)
            layout["gauge"] = (M, gauge_y, available, 28)
            layout["footer"] = None
            panel_y = gauge_y + 28 + 12
            layout["panel"] = (0, panel_y, W, H - panel_y)
            inner_x, inner_w = M, W - 2 * M
            nav_y = panel_y + 12
            toast_h = 38
        layout["toast"] = (inner_x, H - M - toast_h, inner_w, toast_h)
        layout["nav"] = (inner_x, nav_y, inner_w, 38)

        tab_widths = [_text_size(label, 0.42)[0] + 26 for _, label in self._tab_labels()]
        layout["collapsed"] = sum(tab_widths) + 6 * (len(tab_widths) - 1) + 6 > inner_w
        viewport_y = nav_y + 38 + 14
        layout["viewport"] = (inner_x, viewport_y, inner_w, max(40, layout["toast"][1] - 12 - viewport_y))
        return layout

    # --- Static canvas -------------------------------------------------------------------------

    def _rebuild_base_canvas(self, state: str) -> None:
        """Redraws everything that doesn't change every frame."""
        self.clickables = []
        self._map = None
        W, H = self.canvas_w, self.canvas_h
        canvas = np.empty((H, W, 3), dtype=np.uint8)
        canvas[:] = BG
        layout = self._compute_layout(state)
        self._layout = layout
        self._ox = self._oy = 0
        self._clip = None

        self._draw_header(canvas, layout, state)
        self._draw_preview_frame(canvas, layout, state)

        px, py, pw, ph = layout["panel"]
        canvas[py:py + ph, px:px + pw] = SURFACE
        if layout["stacked"]:
            cv2.line(canvas, (0, py), (W, py), BORDER, 1)
        else:
            cv2.line(canvas, (px, py), (px, H), BORDER, 1)

        if layout["footer"] is not None:
            fx, fy, fw = layout["footer"]
            _put(canvas, _fit(self._hotkey_summary("   |   "), fw, 0.4), fx, fy, 0.4, TEXT_DIM)

        self._draw_nav(canvas, layout)
        self._draw_content(canvas, layout)
        if self.menu_open and layout["collapsed"]:
            self._draw_menu_dropdown(canvas, layout)

        self.base_canvas = canvas
        self.dirty = self._relayout
        self._relayout = False
        self._last_state = state

    def _state_color(self, state: str):
        return {"RECORDING": ACCENT_RED, "PAUSED": ACCENT_AMBER}.get(state, ACCENT)

    def _draw_header(self, canvas: np.ndarray, layout: Dict, state: str) -> None:
        W = self.canvas_w
        header_h = layout["header_h"]
        canvas[:header_h] = SURFACE
        canvas[:3] = self._state_color(state)  # Thin state-coloured strip along the top
        cv2.line(canvas, (0, header_h - 1), (W, header_h - 1), BORDER, 1)

        if layout["brand"]:
            cv2.circle(canvas, (MARGIN + 11, 31), 10, ACCENT, 2, cv2.LINE_AA)
            cv2.circle(canvas, (MARGIN + 11, 31), 4, ACCENT_RED, -1, cv2.LINE_AA)
            _put(canvas, "RECORDER STUDIO", MARGIN + 30, 37, 0.58, TEXT_WHITE, FONT_BOLD)

        for spec, rect in zip(self._header_actions(state), layout["actions"]):
            self._draw_button(canvas, rect, spec["label"], spec["action"], active=False,
                              style=spec.get("style", "default"), scale=0.46, icon=spec.get("icon"))

    def _draw_preview_frame(self, canvas: np.ndarray, layout: Dict, state: str) -> None:
        x, y, w, h = layout["preview"]
        _shadow(canvas, x, y, w, h, 10, BG, depth=8)
        _rounded_rect(canvas, x, y, w, h, 10, SURFACE)
        _rounded_rect(canvas, x, y, w, h, 10, BORDER, 1)
        _rounded_rect(canvas, x + 8, y + 8, w - 16, h - 16, 6, SCREEN)
        # The glow around the live image only changes with the layout, the source's size and the
        # state, so it's drawn here once instead of on every UI tick.
        self._baked_glow = None
        if self._preview_src is not None:
            fit = self._fit_preview(layout, *self._preview_src)
            ox, oy, sw, sh = fit
            _glow(canvas, ox, oy, sw, sh, 2, SCREEN, self._state_color(state), layers=3)
            self._baked_glow = (fit, state)

    def _fit_preview(self, layout: Dict, src_w: int, src_h: int) -> Rect:
        """Where the live image goes inside the preview card (aspect-fit), cached per size."""
        x, y, w, h = layout["preview"]
        ix, iy, iw, ih = x + 14, y + 14, w - 28, h - 28
        fit_key = (src_w, src_h, ix, iy, iw, ih)
        if fit_key != self._preview_fit_key:
            scale = min(iw / src_w, ih / src_h)
            sw, sh = max(1, int(src_w * scale)), max(1, int(src_h * scale))
            self._preview_fit = (ix + (iw - sw) // 2, iy + (ih - sh) // 2, sw, sh)
            self._preview_fit_key = fit_key
        return self._preview_fit

    def _draw_nav(self, canvas: np.ndarray, layout: Dict) -> None:
        x, y, w, h = layout["nav"]
        labels = self._tab_labels()
        if layout["collapsed"]:
            active_label = next(label for tab, label in labels if tab == self.active_tab)
            self._draw_button(canvas, (x, y, w, h), f"MENU   {active_label}", "menu_toggle",
                              active=self.menu_open, scale=0.44, icon="menu", align="left",
                              caret="up" if self.menu_open else "down")
            return

        # Segmented tab bar: the active tab is filled with the accent colour.
        _rounded_rect(canvas, x, y, w, h, 9, BG)
        _rounded_rect(canvas, x, y, w, h, 9, BORDER, 1)
        widths = [_text_size(label, 0.42)[0] + 26 for _, label in labels]
        extra = (w - 6 - 6 * (len(widths) - 1) - sum(widths)) // len(widths)
        bx = x + 3
        for (tab, label), tw in zip(labels, widths):
            bw = tw + extra
            self._draw_button(canvas, (bx, y + 3, bw, h - 6), label, f"tab_{tab.lower()}",
                              active=(self.active_tab == tab), style="tab", scale=0.42)
            bx += bw + 6

    def _draw_menu_dropdown(self, canvas: np.ndarray, layout: Dict) -> None:
        x, y, w, h = layout["nav"]
        labels = self._tab_labels()
        item_h = 40
        top = y + h + 6
        total_h = item_h * len(labels) + 8
        _shadow(canvas, x, top, w, total_h, 8, SURFACE, depth=8)
        _rounded_rect(canvas, x, top, w, total_h, 8, CARD)
        _rounded_rect(canvas, x, top, w, total_h, 8, BORDER_STRONG, 1)
        for i, (tab, label) in enumerate(labels):
            self._draw_button(canvas, (x + 4, top + 4 + i * item_h, w - 8, item_h - 4), label, f"tab_{tab.lower()}",
                              active=(self.active_tab == tab), style="menuitem", scale=0.44, align="left")

    def _draw_content(self, canvas: np.ndarray, layout: Dict) -> None:
        """Draws the active tab into a tall image and shows the scrolled part in the viewport."""
        vx, vy, vw, vh = layout["viewport"]
        tab = self.active_tab
        scroll = max(0, min(self.scroll[tab], max(0, self._content_h[tab] - vh)))
        self.scroll[tab] = scroll

        content = np.empty((max(CONTENT_MAX_H, scroll + vh), vw, 3), dtype=np.uint8)
        content[:] = SURFACE
        self._ox, self._oy, self._clip = vx, vy - scroll, (vx, vy, vw, vh)
        try:
            if tab in ("RECORDINGS", "CLIPS"):
                end_y = self._draw_files_tab(content, vw)
            elif tab == "SOURCE":
                end_y = self._draw_source_tab(content, vw)
            else:
                end_y = self._draw_settings_tab(content, vw)
        finally:
            self._ox = self._oy = 0
            self._clip = None

        content_h = min(content.shape[0], end_y + 10)
        if content_h != self._content_h[tab]:
            self._content_h[tab] = content_h
            if scroll > max(0, content_h - vh):
                self._relayout = True  # Content got shorter; redraw with a corrected scroll
        canvas[vy:vy + vh, vx:vx + vw] = content[scroll:scroll + vh]

        if content_h > vh:
            track_x = vx + vw + 6
            thumb_h = max(24, int(vh * vh / content_h))
            thumb_y = vy + int((vh - thumb_h) * scroll / max(1, content_h - vh))
            _rounded_rect(canvas, track_x, vy, 4, vh, 2, CARD)
            _rounded_rect(canvas, track_x, thumb_y, 4, thumb_h, 2, BORDER_STRONG)

    # --- RECORDINGS / CLIPS tab ------------------------------------------------------------------

    def _draw_files_tab(self, img: np.ndarray, cw: int) -> int:
        is_clips = self.active_tab == "CLIPS"
        files = self.clips_files if is_clips else self.recordings_files
        total_mb = sum(f["size_mb"] for f in files)
        label = f"{'Clips' if is_clips else 'Recordings'}  |  {len(files)} files  |  {total_mb:.1f} MB"

        buttons = [
            {"label": "REFRESH", "action": "refresh_files"},
            {"label": "OPEN FOLDER", "action": "open_current_folder"},
        ]
        buttons_w = sum(self._spec_width(b, 0.38) for b in buttons) + 8
        label_w = _text_size(label, 0.42)[0]
        y = 0
        if cw >= label_w + buttons_w + 16:
            _put(img, label, 2, 20, 0.42, TEXT_LIGHT)
            self._buttons_flow(img, cw - buttons_w, 0, buttons_w, buttons, h=28, scale=0.38)
            y = 40
        else:
            _put(img, _fit(label, cw, 0.42), 2, 18, 0.42, TEXT_LIGHT)
            y = self._buttons_flow(img, 0, 28, cw, buttons, h=28, scale=0.38) + 12

        if not files:
            _shadow(img, 0, y, cw, 120, 10, SURFACE)
            _rounded_rect(img, 0, y, cw, 120, 10, CARD)
            _rounded_rect(img, 0, y, cw, 120, 10, BORDER, 1)
            message = f"No {self.active_tab.lower()} yet."
            hint = "Start recording or save a clip to see it here."
            for text, scale, color, dy in ((message, 0.52, TEXT_WHITE, 52), (hint, 0.4, TEXT_GRAY, 80)):
                text = _fit(text, cw - 20, scale)
                tw, _ = _text_size(text, scale)
                _put(img, text, (cw - tw) // 2, y + dy, scale, color)
            return y + 120

        pages = max(1, math.ceil(len(files) / FILES_PER_PAGE))
        page = min(self.clips_page if is_clips else self.recordings_page, pages - 1)
        if is_clips:
            self.clips_page = page
        else:
            self.recordings_page = page
        for item in files[page * FILES_PER_PAGE:(page + 1) * FILES_PER_PAGE]:
            y = self._draw_file_card(img, item, is_clips, cw, y) + 12
        if pages > 1:
            y = self._draw_pagination(img, cw, y, page, pages)
        return y

    def _draw_file_card(self, img: np.ndarray, item: Dict, is_clips: bool, cw: int, y: int) -> int:
        pad = 12
        thumb_w = FILE_THUMB[0] if cw >= 440 else max(96, int(cw * 0.3))
        thumb_h = thumb_w * 9 // 16
        text_x = pad + thumb_w + 14
        text_w = cw - text_x - pad
        filepath = item["path"]

        info = self.media.file_info(filepath, item["mtime"], item["size"])
        if info is None:
            duration_text = "reading..."
        elif info["duration"] is None:
            duration_text = "unknown length"
        else:
            duration_text = _format_duration(info["duration"])

        confirming = self.delete_confirm_path == filepath and (time.time() - self.delete_confirm_time) < 4.0
        buttons = [
            {"label": "PLAY", "action": "play_file", "data": {"path": filepath}, "style": "primary", "icon": "play"},
            {"label": "SHOW IN FOLDER", "action": "show_file", "data": {"path": filepath}},
            {"label": "CONFIRM DELETE" if confirming else "DELETE", "action": "delete_file",
             "data": {"path": filepath}, "style": "danger" if confirming else "default"},
        ]
        rects, buttons_h = _flow([self._spec_width(b, 0.38, 22) for b in buttons], max(60, text_w), 6, 28, 6)
        buttons_y = y + pad + 66
        card_h = max(pad * 2 + thumb_h, buttons_y + buttons_h + pad - y)

        _shadow(img, 0, y, cw, card_h, 10, SURFACE)
        _rounded_rect(img, 0, y, cw, card_h, 10, CARD)
        _rounded_rect(img, 0, y, cw, card_h, 10, BORDER, 1)

        thumb_y = y + (card_h - thumb_h) // 2
        self._draw_thumbnail(img, pad, thumb_y, thumb_w, thumb_h, info["thumb"] if info else None,
                             "loading..." if info is None else "no preview")
        if info is not None and info["duration"] is not None:
            # Duration badge on the thumbnail, like a video site
            badge = _format_duration(info["duration"]).split(".")[0]
            bw = _text_size(badge, 0.36)[0] + 10
            bx, by = pad + thumb_w - bw - 4, thumb_y + thumb_h - 20
            _rounded_rect(img, bx, by, bw, 16, 4, (0, 0, 0))
            _put(img, badge, bx + 5, by + 12, 0.36, TEXT_WHITE)

        tag = "CLIP" if is_clips else "REC"
        tag_color = ACCENT if is_clips else ACCENT_RED
        tag_w = self._pill(img, text_x, y + pad, tag, TEXT_WHITE, _lerp(CARD, tag_color, 0.45), 0.34, tag_color)
        _put(img, _fit(item["name"], text_w - tag_w - 8, 0.44), text_x + tag_w + 8, y + pad + 15, 0.44, TEXT_WHITE)
        _put(img, _fit(f"{item['size_mb']:.2f} MB   |   Length {duration_text}", text_w, 0.4),
             text_x, y + pad + 38, 0.4, TEXT_LIGHT)
        _put(img, _fit(item["time_str"], text_w, 0.36), text_x, y + pad + 56, 0.36, TEXT_GRAY)

        for spec, (rx, ry, rw, rh) in zip(buttons, rects):
            self._draw_button(img, (text_x + rx, buttons_y + ry, rw, rh), spec["label"], spec["action"],
                              spec["data"], style=spec.get("style", "default"), scale=0.38, icon=spec.get("icon"))
        return y + card_h

    def _draw_pagination(self, img: np.ndarray, cw: int, y: int, page: int, pages: int) -> int:
        self._draw_button(img, (0, y, 96, 30), "< PREV", "prev_page", scale=0.4)
        self._draw_button(img, (cw - 96, y, 96, 30), "NEXT >", "next_page", scale=0.4)
        label = f"Page {page + 1} of {pages}"
        tw, _ = _text_size(label, 0.42)
        _put(img, label, (cw - tw) // 2, y + 20, 0.42, TEXT_LIGHT)
        return y + 30

    # --- SOURCE tab -------------------------------------------------------------------------------

    def _draw_source_tab(self, img: np.ndarray, cw: int) -> int:
        """Displays and windows; clicking one switches capture immediately, even mid-recording."""
        y = 0
        card_h = 66 + (36 if self.source_minimized else 0)
        _shadow(img, 0, y, cw, card_h, 10, SURFACE)
        _rounded_rect(img, 0, y, cw, card_h, 10, CARD)
        _rounded_rect(img, 0, y, cw, card_h, 10, ACCENT, 1)
        _rounded_rect(img, 0, y + 10, 4, card_h - 20, 2, ACCENT)
        refresh_w = 92
        self._draw_button(img, (cw - refresh_w - 12, y + 18, refresh_w, 30), "REFRESH", "refresh_sources", scale=0.38)
        _put(img, "NOW CAPTURING", 16, y + 24, 0.36, TEXT_GRAY)
        _put(img, _fit(self.source_label, cw - refresh_w - 44, 0.5, FONT_BOLD), 16, y + 50, 0.5, TEXT_WHITE, FONT_BOLD)
        if self.source_minimized:
            for i, line in enumerate(_wrap("Window is minimized, so its feed is paused. It is restored "
                                           "automatically while recording.", cw - 32, 0.36)[:2]):
                _put(img, line, 16, y + 74 + i * 16, 0.36, ACCENT_AMBER)
        y += card_h + 14

        lock_key = display_name(self.hotkeys.binding("lock_window")) if self.hotkeys else "Unbound"
        for line in _wrap(f"Tip: press [{lock_key}] to capture whichever window is in front.", cw, 0.38):
            _put(img, line, 2, y + 12, 0.38, TEXT_GRAY)
            y += 17
        y += 12

        y = self._draw_title(img, 0, y, cw, "DISPLAYS (FULL SCREEN)")
        for index, display in enumerate(self.displays):
            key = ("display", display["device_idx"], display["output_idx"])
            self._draw_button(img, (0, y, cw, 38), display["label"], f"src_display_{index}",
                              {"display": display}, active=(key == self.source_key), scale=0.42,
                              icon="monitor", align="left")
            y += 44
        y += 12

        y = self._draw_title(img, 0, y, cw, "APPLICATION WINDOWS")
        if not self.window_capture_available:
            return self._draw_notes(img, 0, y, cw, ["Window capture needs the windows-capture package:",
                                                    "pip install windows-capture"])
        if not self.windows:
            return self._draw_notes(img, 0, y, cw, ["No capturable windows found. Click REFRESH."])

        pages = max(1, math.ceil(len(self.windows) / WINDOWS_PER_PAGE))
        self.source_page = min(self.source_page, pages - 1)
        start = self.source_page * WINDOWS_PER_PAGE
        for hwnd, title in self.windows[start:start + WINDOWS_PER_PAGE]:
            y = self._draw_window_row(img, cw, y, hwnd, title) + 8
        if pages > 1:
            y = self._draw_pagination(img, cw, y + 4, self.source_page, pages)
        return y

    def _draw_window_row(self, img: np.ndarray, cw: int, y: int, hwnd: int, title: str) -> int:
        row_h = WINDOW_THUMB[1] + 14
        active = ("window", hwnd) == self.source_key
        hover = self._register((0, y, cw, row_h), f"src_window_{hwnd}", {"hwnd": hwnd, "title": title})
        fill, border, fg, border_w = self._button_colors("default", active, hover)
        _rounded_rect(img, 0, y, cw, row_h, 8, fill)
        _rounded_rect(img, 0, y, cw, row_h, 8, border, border_w)

        ready, thumb = self.media.window_thumbnail(hwnd)
        tw, th = WINDOW_THUMB
        placeholder = "..." if not ready else (title[:1].upper() or "?")
        self._draw_thumbnail(img, 7, y + 7, tw, th, thumb, placeholder, ACCENT if active else None)

        text_x = 7 + tw + 14
        text_w = cw - text_x - 12
        _put(img, _fit(title, text_w, 0.44), text_x, y + 28, 0.44, TEXT_WHITE if (active or hover) else TEXT_LIGHT)
        if active:
            status, color = "Capturing", ACCENT_HOVER
        elif hwnd in self.minimized_windows:
            status, color = "Minimized - restored when selected", ACCENT_AMBER
        else:
            status, color = "Click to capture this window", TEXT_DIM
        _put(img, _fit(status, text_w, 0.36), text_x, y + 50, 0.36, color)
        return y + row_h

    # --- SETTINGS tab ------------------------------------------------------------------------------

    def _draw_settings_tab(self, img: np.ndarray, cw: int) -> int:
        specs = [{"label": page, "action": f"settings_page_{page.lower()}", "active": self.settings_page == page}
                 for page in SETTINGS_PAGES]
        y = self._buttons_grid(img, 0, 0, cw, specs, min_w=110, h=34, scale=0.42) + 20
        if self.settings_page == "OVERLAY":
            return self._draw_overlay_page(img, cw, y)
        if self.settings_page == "HOTKEYS":
            return self._draw_hotkeys_page(img, cw, y)
        if self.settings_page == "VIDEO":
            return self._draw_video_page(img, cw, y)
        if self.settings_page == "AUDIO":
            return self._draw_audio_page(img, cw, y)
        return self._draw_general_page(img, cw, y)

    def _draw_stepper(self, img: np.ndarray, cw: int, y: int, steps: List[Tuple[str, str]],
                      value_text: str, apply_id: str, apply_enabled: bool) -> int:
        """[-big][-small] value [+small][+big] [APPLY]; APPLY moves to its own row when narrow."""
        bw, gap, apply_w = 54, 6, 100
        one_row = cw >= 4 * bw + 120 + apply_w + 5 * gap
        value_w = cw - (4 * bw + 4 * gap + ((apply_w + gap) if one_row else 0))
        x = 0
        for label, action in steps[:2]:
            self._draw_button(img, (x, y, bw, 34), label, action, scale=0.4)
            x += bw + gap
        self._draw_value_box(img, (x, y, value_w, 34), value_text)
        x += value_w + gap
        for label, action in steps[2:]:
            self._draw_button(img, (x, y, bw, 34), label, action, scale=0.4)
            x += bw + gap
        apply_rect = (x, y, apply_w, 34) if one_row else (0, y + 40, cw, 34)
        self._draw_button(img, apply_rect, "APPLY" if apply_enabled else "APPLIED", apply_id,
                          style="primary" if apply_enabled else "default", scale=0.42)
        return y + (34 if one_row else 74)

    def _draw_general_page(self, img: np.ndarray, cw: int, y: int) -> int:
        y = self._draw_title(img, 0, y, cw, f"CAPTURE FRAMERATE ({config.FPS_MIN}-{config.FPS_MAX} FPS)",
                             f"Active: {self.target_fps} FPS. Applying while recording starts a new file segment.")
        y = self._draw_stepper(
            img, cw, y,
            [("-10", "fps_step_-10"), ("-1", "fps_step_-1"), ("+1", "fps_step_1"), ("+10", "fps_step_10")],
            f"{self.pending_fps} FPS", "fps_apply", self.pending_fps != self.target_fps,
        ) + 8
        y = self._buttons_grid(img, 0, y, cw, [
            {"label": str(value), "action": f"fps_preset_{value}", "active": self.pending_fps == value}
            for value in FPS_PRESETS
        ], min_w=64) + 26

        y = self._draw_title(img, 0, y, cw, "INSTANT REPLAY DURATION (UP TO 60 MIN)",
                             f"Active: {self.duration_label(self.buffer_seconds)}. "
                             "Long buffers are kept on disk automatically.")
        y = self._buttons_grid(img, 0, y, cw, [
            {"label": self.duration_label(value).replace(" min", "m"), "action": f"buf_preset_{value}",
             "active": self.pending_buffer_seconds == value}
            for value in DURATION_PRESETS
        ], min_w=58) + 8
        y = self._draw_stepper(
            img, cw, y,
            [("-1m", "buf_step_-60"), ("-10s", "buf_step_-10"), ("+10s", "buf_step_10"), ("+1m", "buf_step_60")],
            self.duration_label(self.pending_buffer_seconds), "buf_apply",
            self.pending_buffer_seconds != self.buffer_seconds,
        ) + 10
        where = {"ram": "kept in RAM", "disk": "kept on disk (Temp/replay_ring)"}.get(self.replay_tier, "off")
        y = self._draw_toggle(img, 0, y, cw, f"Instant replay buffer: {where}", self.replay_tier != "off",
                              "toggle_replay") + 26

        y = self._draw_title(img, 0, y, cw, "WATERMARK & PREVIEW")
        y = self._draw_toggle(img, 0, y, cw, "Burn FPS counter into recorded video", self.burn_fps_on_video,
                              "toggle_burn_fps") + 8
        y = self._draw_toggle(img, 0, y, cw, "Show live FPS on the preview", self.show_fps_on_preview,
                              "toggle_show_fps") + 26

        y = self._draw_title(img, 0, y, cw, "SYSTEM INFO")
        lines = [
            f"Video encoder: {clip_encoder_name()} (asynchronous writer thread)",
            f"Output: {self.canvas_size[0]}x{self.canvas_size[1]}, {self.quality_label()}",
            f"Recordings folder: {self.recordings_dir.resolve()}",
            f"Clips folder: {self.clips_dir.resolve()}",
        ]
        card_h = 18 + len(lines) * 20
        _rounded_rect(img, 0, y, cw, card_h, 8, CARD)
        _rounded_rect(img, 0, y, cw, card_h, 8, BORDER, 1)
        for i, line in enumerate(lines):
            _put(img, _fit(line, cw - 28, 0.37), 14, y + 24 + i * 20, 0.37, TEXT_LIGHT)
        return y + card_h

    def _draw_video_page(self, img: np.ndarray, cw: int, y: int) -> int:
        native_w, native_h = self.native_size
        pending_w, pending_h = self._pending_canvas()
        y = self._draw_title(
            img, 0, y, cw, "OUTPUT RESOLUTION",
            f"Display: {native_w}x{native_h}. Recording at {self.canvas_size[0]}x{self.canvas_size[1]}. "
            "Frames are scaled once right after capture (OpenCV INTER_LINEAR), so the encoder, the "
            "replay buffer and the preview all handle the smaller image.",
        )
        specs = []
        for key, label in RESOLUTION_LABELS.items():
            height = config.OUTPUT_RESOLUTIONS.get(key)
            if height is not None and height >= native_h:
                continue  # Never upscale: presets at or above the display's height are hidden
            specs.append({"label": label, "action": f"res_{key}", "active": self.pending_resolution == key})
        y = self._buttons_grid(img, 0, y, cw, specs, min_w=82, h=32) + 8
        y = self._draw_notes(img, 0, y, cw, [f"Selected: {pending_w}x{pending_h}"]) + 18

        y = self._draw_title(
            img, 0, y, cw, "QUALITY / BITRATE",
            "Presets are set for 1080p at 30 FPS and scale with the resolution and frame rate. "
            "Use the - / + buttons for an exact bitrate.",
        )
        y = self._buttons_grid(img, 0, y, cw, [
            {"label": label, "action": f"quality_{key}", "active": self.pending_quality == key}
            for key, label in QUALITY_LABELS.items()
        ], min_w=82, h=32) + 8
        kbps = self._pending_bitrate_kbps()
        y = self._draw_stepper(
            img, cw, y,
            [("-5", "bitrate_step_-5000"), ("-1", "bitrate_step_-1000"),
             ("+1", "bitrate_step_1000"), ("+5", "bitrate_step_5000")],
            f"{kbps / 1000:.1f} Mbps", "video_apply", self._video_pending(),
        ) + 10
        y = self._draw_notes(img, 0, y, cw, [
            f"{QUALITY_LABELS.get(self.pending_quality, '')}: about {kbps / 1000:.1f} Mbps at "
            f"{pending_w}x{pending_h}, {self.target_fps} FPS "
            f"(~{kbps / 8 / 1000 * 60:.0f} MB per minute of video).",
        ]) + 18

        y = self._draw_title(img, 0, y, cw, "ENCODER")
        return self._draw_notes(img, 0, y, cw, [
            f"Encoder: {clip_encoder_name()}. Chosen by measuring each H.264 encoder at these settings; "
            "the writer thread encodes, so capture never waits on it.",
            "Each new screen image is stored once with its real timestamp, so a busy encoder skips "
            "frames evenly instead of freezing. If no H.264 encoder works, OpenCV's mp4v is used, "
            "which ignores the bitrate.",
            "Applying while recording continues in a new file segment, and the disk replay buffer restarts.",
        ])

    def _draw_audio_page(self, img: np.ndarray, cw: int, y: int) -> int:
        audio = self.audio
        if audio is None or not config.AUDIO_ENABLED:
            y = self._draw_title(img, 0, y, cw, "AUDIO SOURCES")
            return self._draw_notes(img, 0, y, cw, ["Audio is turned off (AUDIO_ENABLED in config.py)."])

        mic_key = display_name(self.hotkeys.binding("mic_toggle")) if self.hotkeys else "Unbound"
        y = self._draw_title(img, 0, y, cw, "MICROPHONE",
                             f"The default Windows recording device. Hotkey: [{mic_key}] (rebind under Hotkeys).")
        y = self._draw_toggle(img, 0, y, cw, "Record the microphone", config.AUDIO_MIC_ENABLED, "mic_toggle") + 24

        mode = config.AUDIO_DESKTOP_MODE
        subtitle = {
            "all": "Everything you hear, except apps marked BLOCKED below.",
            "selected": "Only the apps marked INCLUDED below, each captured by process ID.",
            "off": "No desktop or application audio.",
        }[mode]
        y = self._draw_title(img, 0, y, cw, "DESKTOP & APPLICATION AUDIO", subtitle)
        y = self._buttons_grid(img, 0, y, cw, [
            {"label": label, "action": f"desktop_mode_{key}", "active": mode == key}
            for key, label in DESKTOP_MODE_LABELS.items()
        ], min_w=150, h=32) + 22

        y = self._draw_title(img, 0, y, cw, "APPLICATIONS")
        y = self._buttons_flow(img, 0, y, cw, [{"label": "REFRESH LIST", "action": "refresh_audio_apps"}],
                               h=30, scale=0.38) + 10
        if not audio.process_loopback_supported:
            y = self._draw_notes(img, 0, y, cw, [
                "Per-application audio needs Windows 10 build 20348 or Windows 11. Desktop audio and "
                "the microphone still work."]) + 6
        elif mode == "all" and len(config.AUDIO_BLOCKED_APPS) > 1:
            y = self._draw_notes(img, 0, y, cw, [
                "Windows can exclude only one app per stream, so with several apps blocked every other "
                "app that has audio is recorded on its own and mixed."]) + 6

        catalog = audio.catalog
        if not catalog:
            y = self._draw_notes(img, 0, y, cw, ["Looking for running applications..."]) + 10
        else:
            pages = max(1, math.ceil(len(catalog) / APPS_PER_PAGE))
            self.audio_apps_page = min(self.audio_apps_page, pages - 1)
            start = self.audio_apps_page * APPS_PER_PAGE
            for app in catalog[start:start + APPS_PER_PAGE]:
                y = self._draw_audio_app_row(img, cw, y, app, mode) + 6
            if pages > 1:
                y = self._draw_pagination(img, cw, y + 4, self.audio_apps_page, pages) + 6
        y += 18

        y = self._draw_title(img, 0, y, cw, "TRACKS IN SAVED FILES",
                             "Every source is captured as its own track and placed by its own timestamps, "
                             "so each one stays in sync with the video.")
        y = self._buttons_grid(img, 0, y, cw, [
            {"label": label, "action": f"track_layout_{key}", "active": config.AUDIO_TRACK_LAYOUT == key}
            for key, label in TRACK_LAYOUT_LABELS.items()
        ], min_w=190, h=32) + 22

        y = self._draw_title(img, 0, y, cw, "LIVE SOURCES")
        statuses = sorted(audio.track_status.items(), key=lambda item: track_sort_key(item[0]))
        if not statuses:
            return self._draw_notes(img, 0, y, cw, ["No audio sources are active."])
        for track, status in statuses:
            live = status == "live"
            _rounded_rect(img, 0, y, cw, 30, 6, CARD)
            cv2.circle(img, (16, y + 15), 5, ACCENT_GREEN if live else ACCENT_RED, -1, cv2.LINE_AA)
            text = track_label(track) + ("" if live else f": {status}")
            _put(img, _fit(text, cw - 40, 0.4), 30, y + 20, 0.4, TEXT_LIGHT if live else ACCENT_AMBER)
            y += 36
        return y

    def _draw_audio_app_row(self, img: np.ndarray, cw: int, y: int, app: Dict, mode: str) -> int:
        row_h = 50
        exe = app["exe"]
        _rounded_rect(img, 0, y, cw, row_h, 8, CARD)
        _rounded_rect(img, 0, y, cw, row_h, 8, BORDER, 1)

        if mode == "selected":
            on = exe in config.AUDIO_INCLUDED_APPS
            label, style = ("INCLUDED", "primary") if on else ("NOT INCLUDED", "default")
        elif mode == "all":
            on = exe in config.AUDIO_BLOCKED_APPS
            label, style = ("BLOCKED", "danger") if on else ("RECORDED", "default")
        else:
            label = style = None
        button_w = 128 if cw >= 380 else 104
        if label is not None and self.audio.process_loopback_supported:
            self._draw_button(img, (cw - button_w - 10, y + 10, button_w, 30), label, "audio_app_rule",
                              {"exe": exe}, active=(style == "default" and mode == "all"), style=style, scale=0.36)

        if app["playing"]:
            status, color = "Playing now", ACCENT_GREEN
        elif app["has_audio"]:
            status, color = "Has played audio", TEXT_GRAY
        elif not app["running"]:
            status, color = "Not running", TEXT_DIM
        else:
            status, color = "Running", TEXT_DIM
        if app["title"]:
            status = f"{status} - {app['title']}"
        text_w = cw - button_w - 34
        _put(img, _fit(exe, text_w, 0.44), 14, y + 21, 0.44, TEXT_WHITE)
        _put(img, _fit(status, text_w, 0.35), 14, y + 40, 0.35, color)
        return y + row_h

    def _draw_overlay_page(self, img: np.ndarray, cw: int, y: int) -> int:
        y = self._draw_title(img, 0, y, cw, "LIVE OVERLAY",
                             "Shown on your monitor only. It is excluded from capture and never recorded.")
        y = self._draw_toggle(img, 0, y, cw, "Show the live overlay", config.OVERLAY_ENABLED, "overlay_toggle") + 24

        y = self._draw_title(img, 0, y, cw, "SHOWN STATS")
        y = self._buttons_grid(img, 0, y, cw, [
            {"label": f"{label}: {'ON' if config.OVERLAY_STATS.get(key) else 'OFF'}",
             "action": f"overlay_stat_{key}", "active": bool(config.OVERLAY_STATS.get(key))}
            for key, label in OVERLAY_STAT_LABELS.items()
        ], min_w=170, h=32) + 24

        y = self._draw_title(img, 0, y, cw, "POSITION")
        y = self._buttons_grid(img, 0, y, cw, [
            {"label": label, "action": f"overlay_pos_{key}", "active": config.OVERLAY_POSITION == key}
            for key, label in OVERLAY_POSITION_LABELS.items()
        ], min_w=96, h=32, scale=0.38) + 18

        y = self._draw_title(img, 0, y, cw, "DRAG TO POSITION",
                             "Drag the box on the screen map (or click where it should go). "
                             "The X/Y position is saved and the overlay moves there.")
        y = self._draw_overlay_map(img, cw, y) + 20

        y = self._draw_title(img, 0, y, cw, "MONITOR")
        y = self._buttons_grid(img, 0, y, cw, [
            {"label": "Monitor Being Captured", "action": "overlay_mon_source", "active": config.OVERLAY_MONITOR == "source"},
            {"label": "Primary Monitor", "action": "overlay_mon_primary", "active": config.OVERLAY_MONITOR == "primary"},
        ], min_w=190, h=32) + 18

        return self._draw_notes(img, 0, y, cw, [
            "Uses WDA_EXCLUDEFROMCAPTURE (Windows 10 2004 or later). If Windows can't confirm the "
            "exclusion, the overlay turns off instead of risking appearing in a recording.",
        ])

    def _draw_overlay_map(self, img: np.ndarray, cw: int, y: int) -> int:
        """A miniature of the overlay's monitor with a draggable mock of the overlay."""
        with win32_api.per_monitor_dpi():
            left, top, right, bottom = resolve_monitor_rect(config.OVERLAY_MONITOR, self.source_hwnd, self.monitor_device)
        monitor_w, monitor_h = max(1, right - left), max(1, bottom - top)

        map_w = cw - 12
        map_h = int(map_w * monitor_h / monitor_w)
        if map_h > 240:
            map_h = 240
            map_w = int(map_h * monitor_w / monitor_h)
        mx, my = (cw - map_w) // 2, y + 8
        scale = map_w / monitor_w

        size = overlay_size(overlay_line_count(config.OVERLAY_STATS, self._hotkey_line_count()))
        if self._drag is not None:
            position = self._drag["pos"]
        else:
            position = overlay_origin(config.OVERLAY_POSITION, (config.OVERLAY_X, config.OVERLAY_Y),
                                      (0, 0, monitor_w, monitor_h), size)

        # Bezel and screen
        _shadow(img, mx - 6, my - 6, map_w + 12, map_h + 12, 10, SURFACE)
        _rounded_rect(img, mx - 6, my - 6, map_w + 12, map_h + 12, 10, (8, 8, 8))
        _rounded_rect(img, mx - 6, my - 6, map_w + 12, map_h + 12, 10, BORDER_STRONG, 1)
        screen = img[my:my + map_h, mx:mx + map_w]
        screen[:] = (70, 44, 26)
        for i in range(1, 4):  # Faint thirds grid for orientation
            cv2.line(img, (mx + map_w * i // 4, my), (mx + map_w * i // 4, my + map_h), (84, 56, 36), 1)
            cv2.line(img, (mx, my + map_h * i // 4), (mx + map_w, my + map_h * i // 4), (84, 56, 36), 1)

        hover = self._register((mx, my, map_w, map_h), "overlay_map")
        box_w = max(16, int(size[0] * scale))
        box_h = max(10, int(size[1] * scale))
        bx = mx + min(map_w - box_w, int(position[0] * scale))
        by = my + min(map_h - box_h, int(position[1] * scale))
        region = img[by:by + box_h, bx:bx + box_w]
        region[:] = (region * 0.25).astype(np.uint8)  # Translucent black, like the real overlay
        lines = overlay_line_count(config.OVERLAY_STATS, self._hotkey_line_count())
        line_step = max(3, (box_h - 6) // max(1, lines))
        for i in range(lines):
            ly = by + 4 + i * line_step + line_step // 2
            if ly < by + box_h - 2:
                cv2.line(img, (bx + 4, ly), (bx + 4 + int((box_w - 8) * (0.9 - 0.15 * (i % 3))), ly), TEXT_LIGHT, 1)
        dragging = self._drag is not None
        cv2.rectangle(img, (bx, by), (bx + box_w, by + box_h), ACCENT_HOVER if (hover or dragging) else ACCENT, 2)

        self._map = {
            "origin": (mx + self._ox, my + self._oy),
            "scale": scale,
            "size": size,
            "monitor_size": (monitor_w, monitor_h),
            "mock": (bx + self._ox, by + self._oy, box_w, box_h),
            "pos": tuple(position),
        }

        y = my + map_h + 14
        mode = "custom" if config.OVERLAY_POSITION == "custom" else OVERLAY_POSITION_LABELS[config.OVERLAY_POSITION].lower()
        caption = f"X {position[0]}   Y {position[1]}   ({mode}, on a {monitor_w}x{monitor_h} display)"
        tw, _ = _text_size(_fit(caption, cw, 0.4), 0.4)
        _put(img, _fit(caption, cw, 0.4), (cw - tw) // 2, y + 14, 0.4, TEXT_LIGHT)
        return y + 20

    def _draw_hotkeys_page(self, img: np.ndarray, cw: int, y: int) -> int:
        y = self._draw_title(img, 0, y, cw, "GLOBAL HOTKEYS",
                             "Work in any application. Click REBIND, then press the new keys.")
        if self.hotkeys is None:
            return y
        inline = cw >= 420
        for action, label in config.HOTKEY_LABELS.items():
            card_h = 60 if inline else 98
            rebinding = self.rebinding_action == action
            _rounded_rect(img, 0, y, cw, card_h, 8, CARD)
            _rounded_rect(img, 0, y, cw, card_h, 8, ACCENT_AMBER if rebinding else BORDER, 2 if rebinding else 1)
            text_w = cw - (200 if inline else 28)
            _put(img, _fit(label, text_w, 0.42), 14, y + 22, 0.42, TEXT_WHITE)
            if rebinding:
                _put(img, _fit("Press keys... (Esc cancels)", text_w, 0.4), 14, y + 46, 0.4, ACCENT_AMBER)
            else:
                combo = display_name(self.hotkeys.binding(action))
                self._pill(img, 14, y + 31, _fit(combo, text_w - 20, 0.4), TEXT_WHITE, SCREEN, 0.4, BORDER_STRONG)
            if inline:
                bx, by = cw - 184, y + 14
                self._draw_button(img, (bx, by, 98, 32), "REBIND", f"hk_rebind_{action}", active=rebinding,
                                  style="primary" if not rebinding else "default", scale=0.38)
                self._draw_button(img, (bx + 104, by, 70, 32), "CLEAR", f"hk_clear_{action}", scale=0.38)
            else:
                self._buttons_grid(img, 14, y + 58, cw - 28, [
                    {"label": "REBIND", "action": f"hk_rebind_{action}", "active": rebinding,
                     "style": "default" if rebinding else "primary"},
                    {"label": "CLEAR", "action": f"hk_clear_{action}"},
                ], min_w=80, h=30, scale=0.38)
            y += card_h + 10

        return self._draw_notes(img, 0, y + 6, cw, [
            "Esc exits the app only while the Studio window has focus.",
            "Single keys (like P) still type normally in other apps.",
            "Bindings are saved to user_settings.json.",
        ])

    # --- Per-frame render ----------------------------------------------------------------------------

    def render(
        self,
        raw_screen_frame: np.ndarray,
        current_fps: float,
        active_elapsed_sec: float,
        buffer_len: int,
        buffer_max: int,
        state: str = "IDLE",
    ) -> np.ndarray:
        """Assembles the studio window frame: cached layout plus the live parts."""
        self.state = state
        self._apply_pending_rebind()
        if self.media.version != self._media_version:
            self._media_version = self.media.version
            self.dirty = True  # New thumbnails or durations arrived
        if self.audio is not None:
            versions = (self.audio.catalog_version, self.audio.status_version)
            if versions != self._audio_versions:
                self._audio_versions = versions
                self.dirty = True  # App list or live audio sources changed

        if self.dirty or state != self._last_state:
            self._rebuild_base_canvas(state)

        canvas = self.base_canvas.copy()
        layout = self._layout
        self._draw_hud(canvas, layout, state, current_fps, active_elapsed_sec, raw_screen_frame.shape)
        self._draw_preview(canvas, layout, raw_screen_frame, current_fps, state)
        self._draw_gauge(canvas, layout, buffer_len, buffer_max)
        self._draw_toast(canvas, layout)
        return canvas

    def _draw_hud(self, canvas: np.ndarray, layout: Dict, state: str, fps: float, elapsed: float, shape) -> None:
        cx, cy = layout["chip"]
        if state == "RECORDING":
            color, text = ACCENT_RED, "REC"
            pulse = 0.55 + 0.45 * math.sin(time.time() * 5.0)
            dot = _lerp(ACCENT_RED_SOFT, ACCENT_RED, pulse)
        elif state == "PAUSED":
            color, text, dot = ACCENT_AMBER, "PAUSED", ACCENT_AMBER
        else:
            color, text, dot = ACCENT_GREEN, "READY", ACCENT_GREEN
        _rounded_rect(canvas, cx, cy, 104, 28, 14, _lerp(SURFACE, color, 0.2))
        _rounded_rect(canvas, cx, cy, 104, 28, 14, color, 1)
        cv2.circle(canvas, (cx + 17, cy + 14), 5, dot, -1, cv2.LINE_AA)
        _put(canvas, text, cx + 30, cy + 19, 0.44, TEXT_WHITE)

        seconds = int(elapsed)
        tx, ty = layout["timer"]
        _put(canvas, f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}", tx, ty,
             0.74, TEXT_WHITE, FONT_BOLD)

        fx, fy = layout["fps"]
        if layout["fps_w"] > 60:
            src_h, src_w = shape[:2]
            text = _fit(f"{fps:5.1f} / {self.target_fps} FPS   {src_w}x{src_h}", layout["fps_w"], 0.46)
            _put(canvas, text, fx, fy, 0.46, TEXT_LIGHT)

    def _draw_preview(self, canvas: np.ndarray, layout: Dict, frame: np.ndarray, fps: float, state: str) -> None:
        src_h, src_w = frame.shape[:2]
        self._preview_src = (src_w, src_h)
        fit = self._fit_preview(layout, src_w, src_h)
        ox, oy, sw, sh = fit

        color = self._state_color(state)
        if self._baked_glow != (fit, state):
            # The source size changed since the layout was drawn: draw the glow this once
            # and bake it into the cached layout on the next tick.
            _glow(canvas, ox, oy, sw, sh, 2, SCREEN, color, layers=3)
            self.dirty = True
        canvas[oy:oy + sh, ox:ox + sw] = cv2.resize(frame, (sw, sh), interpolation=cv2.INTER_LINEAR)
        cv2.rectangle(canvas, (ox - 2, oy - 2), (ox + sw + 1, oy + sh + 1), color, 2)

        if sw > 160 and sh > 60:
            label = _fit(self.source_label or "Capture", sw - 150, 0.4)
            self._pill(canvas, ox + 10, oy + 10, label, TEXT_WHITE, (10, 10, 10), 0.4, BORDER_STRONG)
            if self.show_fps_on_preview:
                self._pill(canvas, ox + sw - 10, oy + 10, f"LIVE {fps:.1f} FPS", TEXT_WHITE, (10, 10, 10), 0.4,
                           color, align_right=True)
            if self.source_minimized:
                message = _fit("Window minimized: feed paused (restored automatically while recording)", sw - 40, 0.4)
                pw = _text_size(message, 0.4)[0] + 18
                self._pill(canvas, ox + (sw - pw) // 2, oy + sh - 40, message, (20, 20, 20), ACCENT_AMBER, 0.4)

    def _draw_gauge(self, canvas: np.ndarray, layout: Dict, buffer_len: int, buffer_max: int) -> None:
        x, y, w, h = layout["gauge"]
        _rounded_rect(canvas, x, y, w, h, h // 2, CARD)
        if self.replay_tier == "off":
            text, color = "Instant replay is off. Turn it on in Settings > General.", TEXT_GRAY
        else:
            ratio = min(1.0, buffer_len / max(1, buffer_max))
            ready = ratio >= 0.99
            if ratio > 0:
                _rounded_rect(canvas, x, y, max(h, int(w * ratio)), h, h // 2,
                              _lerp(CARD, ACCENT_GREEN if ready else ACCENT, 0.75))
            status = "READY TO CLIP" if ready else f"BUFFERING {int(ratio * 100)}%"
            tier = "RAM" if self.replay_tier == "ram" else "DISK"
            text = (f"Instant replay [{tier}]  {ratio * self.buffer_seconds:.1f}s / {self.buffer_seconds}s  "
                    f"({buffer_len}/{buffer_max} frames)  -  {status}")
            color = TEXT_WHITE
        _rounded_rect(canvas, x, y, w, h, h // 2, BORDER, 1)
        _put(canvas, _fit(text, w - 32, 0.42), x + 16, y + h // 2 + 5, 0.42, color)

    def _draw_toast(self, canvas: np.ndarray, layout: Dict) -> None:
        x, y, w, h = layout["toast"]
        _rounded_rect(canvas, x, y, w, h, 8, CARD)
        _rounded_rect(canvas, x, y, w, h, 8, BORDER, 1)
        _rounded_rect(canvas, x + 6, y + 8, 4, h - 16, 2, self.toast_color)
        _put(canvas, _fit(self.toast_text, w - 34, 0.42), x + 20, y + h // 2 + 5, 0.42, self.toast_color)
