"""
Studio UI Dashboard module.
High-performance interactive management and controls interface with a simple,
clean black and gray-ish contrast aesthetic.
Supports initial idle standby, start/stop recording controls, in-flight settings,
and direct file management for Recordings and Clips.
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


# Minimalist Black & Gray Contrast Palette (BGR format for OpenCV)
BG_BLACK = (16, 16, 16)          # #101010 Deep Matte Black
PANEL_GRAY = (26, 26, 26)        # #1a1a1a Dark Gray Panel Surface
CARD_GRAY = (36, 36, 36)         # #242424 Charcoal Card/Button Background
CARD_HOVER = (50, 50, 50)        # #323232 Lighter Gray Hover
CARD_ACTIVE = (64, 64, 64)       # #404040 Highlight Active Gray
BORDER_DARK = (46, 46, 46)       # #2e2e2e Subtle Gray Border
BORDER_LIGHT = (85, 85, 85)      # #555555 Crisp Contrast Border
BORDER_WHITE = (180, 180, 180)   # #b4b4b4 Focus/Active Border

TEXT_WHITE = (245, 245, 245)     # #f5f5f5 Crisp Primary Text
TEXT_GRAY = (160, 160, 160)      # #a0a0a0 Secondary Muted Text
TEXT_DIM = (105, 105, 105)       # #696969 Dim Text

# Minimal Contextual Accents (subtle and purposeful)
ACCENT_RED = (45, 45, 220)       # Clean Red (Recording indicator / Start dot)
ACCENT_AMBER = (40, 170, 230)    # Subtle Amber (Paused state)
ACCENT_GREEN = (70, 190, 80)     # Subtle Green (Ready / Playing state)


class ClickableArea:
    """Represents a clickable screen bounding box with action identifier."""
    def __init__(self, rect: Tuple[int, int, int, int], action_id: str, data: Optional[dict] = None):
        self.x, self.y, self.w, self.h = rect
        self.action_id = action_id
        self.data = data or {}

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px <= (self.x + self.w) and self.y <= py <= (self.y + self.h)


class StudioDashboard:
    """
    Studio UI Dashboard:
    Simple black and gray-ish contrast interface.
    Operates in IDLE, RECORDING, and PAUSED states without prematurely closing.
    """

    def __init__(
        self,
        recordings_dir: Path,
        clips_dir: Path,
        initial_fps: int = 30,
        initial_buffer_seconds: int = 30,
        burn_fps_initial: bool = False,
    ):
        self.recordings_dir = Path(recordings_dir)
        self.clips_dir = Path(clips_dir)
        self.target_fps = initial_fps
        self.buffer_seconds = initial_buffer_seconds
        self.burn_fps_on_video = burn_fps_initial
        self.show_fps_on_preview = True

        # Application state: "IDLE", "RECORDING", "PAUSED"
        self.state = "IDLE"

        # Navigation & UI State
        self.active_tab = "RECORDINGS"
        self.recordings_page = 0
        self.clips_page = 0
        self.items_per_page = 4

        # Files cache
        self.recordings_files: List[Dict] = []
        self.clips_files: List[Dict] = []
        self.last_files_scan = 0.0

        # Mouse tracking
        self.mouse_x = -1
        self.mouse_y = -1
        self.hovered_action: Optional[str] = None
        self.clickables: List[ClickableArea] = []

        # Action requests for main loop
        self.request_start_recording = False
        self.request_stop_recording = False
        self.request_pause_toggle = False
        self.request_clip = False
        self.request_exit_app = False
        self.request_fps_change: Optional[int] = None
        self.request_buffer_change: Optional[int] = None

        # Toast / status messages
        self.toast_text: str = "Ready. Click 'START RECORDING' or press F9 to begin."
        self.toast_color: Tuple[int, int, int] = TEXT_GRAY

        # Delete confirmation tracking
        self.delete_confirm_path: Optional[Path] = None
        self.delete_confirm_time: float = 0.0

        # Canvas Dimensions & Caching
        self.canvas_w = 1360
        self.canvas_h = 768
        self.base_canvas = np.full((self.canvas_h, self.canvas_w, 3), BG_BLACK, dtype=np.uint8)
        self.dirty = True
        self._last_state = "IDLE"

        # Preview geometry cache
        self._cached_src_dims = (0, 0)
        self._preview_scale_rect = (0, 0, 0, 0)

        # Initial folder scan & render
        self.refresh_file_lists()

    def set_toast(self, text: str, color: Tuple[int, int, int] = TEXT_GRAY) -> None:
        """Displays a notification toast in the UI."""
        self.toast_text = text
        self.toast_color = color

    def refresh_file_lists(self) -> None:
        """Scans the Recordings and Clips directories and populates metadata."""
        self.recordings_files = self._scan_directory(self.recordings_dir)
        self.clips_files = self._scan_directory(self.clips_dir)
        self.last_files_scan = time.time()
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
                    size_mb = stat.st_size / (1024 * 1024)
                    mtime_dt = datetime.fromtimestamp(stat.st_mtime)
                    entries.append({
                        "path": file,
                        "name": file.name,
                        "size_mb": size_mb,
                        "time_str": mtime_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "mtime": stat.st_mtime,
                    })
                except OSError:
                    continue

        entries.sort(key=lambda x: x["mtime"], reverse=True)
        return entries

    def on_mouse(self, event: int, x: int, y: int, flags: int, param: any) -> None:
        """Handles OpenCV mouse events."""
        self.mouse_x = x
        self.mouse_y = y

        # Check hover changes to trigger redraw only when necessary
        new_hover = None
        for item in self.clickables:
            if item.contains(x, y):
                new_hover = item.action_id
                break

        if new_hover != self.hovered_action:
            self.hovered_action = new_hover
            self.dirty = True

        if event == cv2.EVENT_LBUTTONDOWN:
            for item in self.clickables:
                if item.contains(x, y):
                    self._handle_action(item.action_id, item.data)
                    self.dirty = True
                    break

        elif event == cv2.EVENT_MOUSEWHEEL:
            if x >= 860:
                if flags > 0:
                    self._handle_action("prev_page", {})
                else:
                    self._handle_action("next_page", {})
                self.dirty = True

    def _handle_action(self, action_id: str, data: dict) -> None:
        """Dispatches button clicks."""
        # Top Action Bar
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

        # Tab Navigation
        elif action_id.startswith("tab_"):
            self.active_tab = action_id[4:].upper()
            self.dirty = True

        # Settings
        elif action_id.startswith("fps_"):
            fps_val = int(action_id[4:])
            self.target_fps = fps_val
            self.request_fps_change = fps_val
            self.set_toast(f"Target framerate set to {fps_val} FPS", TEXT_WHITE)
            self.dirty = True

        elif action_id.startswith("buf_"):
            buf_sec = int(action_id[4:])
            self.buffer_seconds = buf_sec
            self.request_buffer_change = buf_sec
            self.set_toast(f"Instant replay buffer adjusted to {buf_sec}s", TEXT_WHITE)
            self.dirty = True

        elif action_id == "toggle_burn_fps":
            self.burn_fps_on_video = not self.burn_fps_on_video
            status = "ENABLED" if self.burn_fps_on_video else "DISABLED"
            self.set_toast(f"Burn FPS watermark on recorded video: {status}", TEXT_WHITE)
            self.dirty = True

        elif action_id == "toggle_show_fps":
            self.show_fps_on_preview = not self.show_fps_on_preview
            status = "ENABLED" if self.show_fps_on_preview else "DISABLED"
            self.set_toast(f"Show FPS on preview HUD: {status}", TEXT_WHITE)
            self.dirty = True

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
                    self.set_toast(f"Click Delete again to confirm deleting '{filepath.name}'", ACCENT_AMBER)
                    self.dirty = True

        # Pagination
        elif action_id == "prev_page":
            if self.active_tab == "RECORDINGS" and self.recordings_page > 0:
                self.recordings_page -= 1
                self.dirty = True
            elif self.active_tab == "CLIPS" and self.clips_page > 0:
                self.clips_page -= 1
                self.dirty = True

        elif action_id == "next_page":
            if self.active_tab == "RECORDINGS":
                max_page = max(0, math.ceil(len(self.recordings_files) / self.items_per_page) - 1)
                if self.recordings_page < max_page:
                    self.recordings_page += 1
                    self.dirty = True
            elif self.active_tab == "CLIPS":
                max_page = max(0, math.ceil(len(self.clips_files) / self.items_per_page) - 1)
                if self.clips_page < max_page:
                    self.clips_page += 1
                    self.dirty = True

    def _draw_button(
        self,
        canvas: np.ndarray,
        rect: Tuple[int, int, int, int],
        text: str,
        action_id: str,
        action_data: Optional[dict] = None,
        is_active: bool = False,
        style: str = "default",
        font_scale: float = 0.44,
    ) -> None:
        """Draws an interactive button in clean black/gray contrast."""
        x, y, w, h = rect
        self.clickables.append(ClickableArea(rect, action_id, action_data))
        is_hover = (self.hovered_action == action_id)

        # Grayscale contrast styles
        if style == "primary":
            bg = CARD_HOVER if is_hover else CARD_GRAY
            border = BORDER_WHITE if is_hover else BORDER_LIGHT
            txt_color = TEXT_WHITE
        elif style == "danger":
            bg = (30, 25, 38) if is_hover else (25, 20, 30)
            border = ACCENT_RED if is_hover else (60, 40, 50)
            txt_color = (200, 200, 245)
        elif style == "active_tab":
            bg = CARD_GRAY
            border = BORDER_WHITE
            txt_color = TEXT_WHITE
        else:
            if is_active:
                bg = CARD_ACTIVE
                border = BORDER_WHITE
                txt_color = TEXT_WHITE
            elif is_hover:
                bg = CARD_HOVER
                border = BORDER_LIGHT
                txt_color = TEXT_WHITE
            else:
                bg = CARD_GRAY
                border = BORDER_DARK
                txt_color = TEXT_GRAY

        cv2.rectangle(canvas, (x, y), (x + w, y + h), bg, -1)
        cv2.rectangle(canvas, (x, y), (x + w, y + h), border, 1, cv2.LINE_AA)

        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, 1)
        tx = x + max(4, (w - tw) // 2)
        ty = y + (h + th) // 2 - 1
        cv2.putText(canvas, text, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, font_scale, txt_color, 1, cv2.LINE_AA)

    def _rebuild_base_canvas(self, state: str) -> None:
        """
        Reconstructs the persistent static canvas layout.
        Uses a minimalist, refined black and gray contrast design.
        """
        self.clickables.clear()
        canvas = np.full((self.canvas_h, self.canvas_w, 3), BG_BLACK, dtype=np.uint8)

        left_w = 860
        right_x = 860
        right_w = self.canvas_w - right_x

        # 1. Left Preview Container & HUD
        cv2.rectangle(canvas, (0, 0), (left_w, 52), PANEL_GRAY, -1)
        cv2.rectangle(canvas, (0, 0), (left_w, 52), BORDER_DARK, 1)

        # Preview placeholder viewport
        cv2.rectangle(canvas, (16, 66), (left_w - 16, 676), (12, 12, 12), -1)
        cv2.rectangle(canvas, (16, 66), (left_w - 16, 676), BORDER_DARK, 1)

        # Replay Buffer gauge container
        cv2.rectangle(canvas, (16, 688), (left_w - 16, 712), CARD_GRAY, -1)
        cv2.rectangle(canvas, (16, 688), (left_w - 16, 712), BORDER_DARK, 1)

        # Bottom hotkeys reminder footer
        hotkeys_msg = "[F9] Start / Stop Recording   |   [~] Clip   |   [P] Pause   |   [ESC] Exit"
        cv2.putText(canvas, hotkeys_msg, (20, 742), cv2.FONT_HERSHEY_SIMPLEX, 0.42, TEXT_DIM, 1, cv2.LINE_AA)

        # 2. Right Side Studio Control Panel
        cv2.rectangle(canvas, (right_x, 0), (self.canvas_w, self.canvas_h), PANEL_GRAY, -1)
        cv2.line(canvas, (right_x, 0), (right_x, self.canvas_h), BORDER_DARK, 1, cv2.LINE_AA)

        # Studio Title
        cv2.putText(canvas, "RECORDER STUDIO", (right_x + 18, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.62, TEXT_WHITE, 2, cv2.LINE_AA)
        cv2.line(canvas, (right_x + 18, 42), (right_x + 155, 42), BORDER_LIGHT, 2, cv2.LINE_AA)

        # Action Control Bar (State Sensitive)
        ctrl_y = 54
        ctrl_h = 38

        if state == "IDLE":
            # Idle Mode: Prominent Start Recording button + Exit App button
            start_btn_w = 330
            exit_btn_w = right_w - start_btn_w - 48

            self._draw_button(
                canvas, (right_x + 16, ctrl_y, start_btn_w, ctrl_h),
                "   START RECORDING", "start_recording", style="primary", font_scale=0.5
            )
            # Red recording dot inside Start button
            cv2.circle(canvas, (right_x + 65, ctrl_y + ctrl_h // 2), 6, ACCENT_RED, -1, cv2.LINE_AA)

            self._draw_button(
                canvas, (right_x + 360, ctrl_y, exit_btn_w, ctrl_h),
                "EXIT", "exit_app", style="default", font_scale=0.44
            )

        else:
            # Active Recording / Paused Mode: Stop, Pause/Resume, and Clip buttons
            btn_w = 150
            btn_gap = 10

            self._draw_button(
                canvas, (right_x + 16, ctrl_y, btn_w, ctrl_h),
                "[X] STOP", "stop_recording", style="danger", font_scale=0.46
            )

            pause_label = "> RESUME" if state == "PAUSED" else "|| PAUSE"
            self._draw_button(
                canvas, (right_x + 16 + btn_w + btn_gap, ctrl_y, btn_w, ctrl_h),
                pause_label, "toggle_pause", style="default", font_scale=0.46
            )

            clip_text = f"CLIP ({self.buffer_seconds}s)"
            self._draw_button(
                canvas, (right_x + 16 + 2 * (btn_w + btn_gap), ctrl_y, btn_w, ctrl_h),
                clip_text, "trigger_clip", style="default", font_scale=0.46
            )

        # 3. Navigation Tabs
        tab_y = 106
        tab_h = 32
        tab_w = 154
        tabs = [
            ("tab_recordings", f"RECORDINGS ({len(self.recordings_files)})", self.active_tab == "RECORDINGS"),
            ("tab_clips", f"CLIPS ({len(self.clips_files)})", self.active_tab == "CLIPS"),
            ("tab_settings", "SETTINGS", self.active_tab == "SETTINGS"),
        ]

        for i, (tab_id, tab_title, is_active) in enumerate(tabs):
            tx = right_x + 16 + i * 156
            tab_style = "active_tab" if is_active else "default"
            self._draw_button(canvas, (tx, tab_y, tab_w, tab_h), tab_title, tab_id, is_active=is_active, style=tab_style, font_scale=0.40)
            if is_active:
                cv2.rectangle(canvas, (tx + 12, tab_y + tab_h - 2), (tx + tab_w - 12, tab_y + tab_h), TEXT_WHITE, -1)

        # 4. Tab Body Content
        content_y = 150
        if self.active_tab in ("RECORDINGS", "CLIPS"):
            self._draw_files_tab(canvas, right_x, content_y)
        else:
            self._draw_settings_tab(canvas, right_x, content_y)

        self.base_canvas = canvas
        self.dirty = False
        self._last_state = state

    def _draw_files_tab(self, canvas: np.ndarray, right_x: int, start_y: int) -> None:
        """Renders file list with grayscale cards and buttons."""
        is_clips = self.active_tab == "CLIPS"
        files_list = self.clips_files if is_clips else self.recordings_files
        current_page = self.clips_page if is_clips else self.recordings_page

        total_size_mb = sum(f["size_mb"] for f in files_list)
        folder_label = f"Folder: {'Clips' if is_clips else 'Recordings'} | {len(files_list)} files ({total_size_mb:.1f} MB)"
        cv2.putText(canvas, folder_label, (right_x + 18, start_y + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.42, TEXT_GRAY, 1, cv2.LINE_AA)

        self._draw_button(canvas, (right_x + 338, start_y, 65, 24), "REFRESH", "refresh_files", font_scale=0.36)
        self._draw_button(canvas, (right_x + 408, start_y, 68, 24), "OPEN DIR", "open_current_folder", font_scale=0.36)

        card_start_y = start_y + 36
        card_h = 100
        card_gap = 12
        card_w = 460

        total_pages = max(1, math.ceil(len(files_list) / self.items_per_page))
        current_page = min(current_page, total_pages - 1)
        start_idx = current_page * self.items_per_page
        visible_files = files_list[start_idx:start_idx + self.items_per_page]

        if not files_list:
            no_files_box_y = card_start_y + 40
            cv2.rectangle(canvas, (right_x + 16, no_files_box_y), (right_x + 16 + card_w, no_files_box_y + 120), CARD_GRAY, -1)
            cv2.rectangle(canvas, (right_x + 16, no_files_box_y), (right_x + 16 + card_w, no_files_box_y + 120), BORDER_DARK, 1)
            msg = f"No {self.active_tab.lower()} found yet."
            cv2.putText(canvas, msg, (right_x + 140, no_files_box_y + 55), cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT_GRAY, 1, cv2.LINE_AA)
            sub = "Start recording or save a clip to see items here."
            cv2.putText(canvas, sub, (right_x + 105, no_files_box_y + 80), cv2.FONT_HERSHEY_SIMPLEX, 0.4, TEXT_DIM, 1, cv2.LINE_AA)
        else:
            for idx, item in enumerate(visible_files):
                cy = card_start_y + idx * (card_h + card_gap)
                cx = right_x + 16
                filepath = item["path"]

                # Card box
                cv2.rectangle(canvas, (cx, cy), (cx + card_w, cy + card_h), CARD_GRAY, -1)
                cv2.rectangle(canvas, (cx, cy), (cx + card_w, cy + card_h), BORDER_DARK, 1)

                # Tag & Filename
                icon_tag = "[CLIP]" if is_clips else "[REC]"
                cv2.putText(canvas, icon_tag, (cx + 12, cy + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.44, TEXT_GRAY, 1, cv2.LINE_AA)

                fname = item["name"]
                if len(fname) > 34:
                    fname = fname[:31] + "..."
                cv2.putText(canvas, fname, (cx + 66, cy + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.44, TEXT_WHITE, 1, cv2.LINE_AA)

                # Metadata
                meta_str = f"Size: {item['size_mb']:.2f} MB   |   Date: {item['time_str']}"
                cv2.putText(canvas, meta_str, (cx + 14, cy + 50), cv2.FONT_HERSHEY_SIMPLEX, 0.38, TEXT_GRAY, 1, cv2.LINE_AA)

                # Action buttons
                btn_y = cy + 62
                btn_h = 28
                self._draw_button(
                    canvas, (cx + 14, btn_y, 115, btn_h), "> PLAY", "play_file",
                    action_data={"path": filepath}, style="default", font_scale=0.4
                )
                self._draw_button(
                    canvas, (cx + 138, btn_y, 140, btn_h), "SHOW IN FOLDER", "show_file",
                    action_data={"path": filepath}, style="default", font_scale=0.38
                )

                is_confirming = (self.delete_confirm_path == filepath) and ((time.time() - self.delete_confirm_time) < 4.0)
                del_label = "CONFIRM DEL?" if is_confirming else "DELETE"
                del_style = "danger" if is_confirming else "default"
                self._draw_button(
                    canvas, (cx + 288, btn_y, 156, btn_h), del_label, "delete_file",
                    action_data={"path": filepath}, style=del_style, font_scale=0.38
                )

        # Pagination Bar
        page_y = 660
        self._draw_button(canvas, (right_x + 16, page_y, 80, 26), "< PREV", "prev_page", font_scale=0.4)
        page_indicator = f"Page {current_page + 1} of {total_pages}"
        cv2.putText(canvas, page_indicator, (right_x + 200, page_y + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.42, TEXT_GRAY, 1, cv2.LINE_AA)
        self._draw_button(canvas, (right_x + 396, page_y, 80, 26), "NEXT >", "next_page", font_scale=0.4)

    def _draw_settings_tab(self, canvas: np.ndarray, right_x: int, start_y: int) -> None:
        """Renders configuration settings in clean black/gray contrast."""
        cx = right_x + 16
        cy = start_y + 10

        # Section 1: Framerate
        cv2.putText(canvas, "TARGET FRAMERATE", (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.48, TEXT_WHITE, 1, cv2.LINE_AA)
        cy += 14
        cv2.putText(canvas, "Adjusts capture rate and pacing.", (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.36, TEXT_GRAY, 1, cv2.LINE_AA)
        cy += 14

        fps_options = [20, 30, 60]
        btn_w = 146
        for i, val in enumerate(fps_options):
            bx = cx + i * 154
            is_active = (self.target_fps == val)
            label = f"{val} FPS" + (" (Active)" if is_active else "")
            self._draw_button(canvas, (bx, cy, btn_w, 36), label, f"fps_{val}", is_active=is_active, font_scale=0.42)

        # Section 2: Replay Buffer Duration
        cy += 60
        cv2.putText(canvas, "INSTANT REPLAY BUFFER DURATION", (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.48, TEXT_WHITE, 1, cv2.LINE_AA)
        cy += 14
        cv2.putText(canvas, "Duration of rolling memory buffer for instant clipping.", (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.36, TEXT_GRAY, 1, cv2.LINE_AA)
        cy += 14

        buf_options = [15, 30, 60]
        for i, val in enumerate(buf_options):
            bx = cx + i * 154
            is_active = (self.buffer_seconds == val)
            label = f"{val} Seconds" + (" (Active)" if is_active else "")
            self._draw_button(canvas, (bx, cy, btn_w, 36), label, f"buf_{val}", is_active=is_active, font_scale=0.4)

        # Section 3: Watermark & Overlays
        cy += 60
        cv2.putText(canvas, "OVERLAY & WATERMARK CONFIGURATION", (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.48, TEXT_WHITE, 1, cv2.LINE_AA)
        cy += 18

        burn_state = "ENABLED (Burned on video)" if self.burn_fps_on_video else "DISABLED (Clean video)"
        self._draw_button(
            canvas, (cx, cy, 460, 34), f"Burn FPS on Video File: {burn_state}",
            "toggle_burn_fps", is_active=self.burn_fps_on_video, font_scale=0.4
        )

        cy += 44
        preview_state = "ENABLED (Visible in HUD)" if self.show_fps_on_preview else "DISABLED"
        self._draw_button(
            canvas, (cx, cy, 460, 34), f"Show Live FPS on Preview HUD: {preview_state}",
            "toggle_show_fps", is_active=self.show_fps_on_preview, font_scale=0.4
        )

        # Section 4: System Info Card
        cy += 58
        card_h = 130
        cv2.rectangle(canvas, (cx, cy), (cx + 460, cy + card_h), CARD_GRAY, -1)
        cv2.rectangle(canvas, (cx, cy), (cx + 460, cy + card_h), BORDER_DARK, 1)
        cv2.putText(canvas, "SYSTEM & SHORTCUTS INFO", (cx + 16, cy + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.44, TEXT_WHITE, 1, cv2.LINE_AA)

        info_lines = [
            f"Encoder Codec: mp4v (.mp4)  |  Async Multi-Threaded",
            f"Recordings Directory: {self.recordings_dir.resolve()}",
            f"Clips Directory: {self.clips_dir.resolve()}",
            f"Controls: [F9] Start/Stop | [P] Pause | [~] Clip | [ESC] Exit",
        ]
        for line_idx, line in enumerate(info_lines):
            if len(line) > 58:
                line = line[:55] + "..."
            cv2.putText(canvas, line, (cx + 16, cy + 48 + line_idx * 22), cv2.FONT_HERSHEY_SIMPLEX, 0.36, TEXT_GRAY, 1, cv2.LINE_AA)

    def render(
        self,
        raw_screen_frame: np.ndarray,
        current_fps: float,
        active_elapsed_sec: float,
        buffer_len: int,
        buffer_max: int,
        state: str = "IDLE",
    ) -> np.ndarray:
        """
        Assembles the studio window frame with sub-4ms performance.
        Reflects current state (IDLE, RECORDING, PAUSED) with simple black and gray contrast.
        """
        self.state = state

        if self.dirty or (state != self._last_state):
            self._rebuild_base_canvas(state)

        # Clone cached background canvas (~0.8ms)
        canvas = self.base_canvas.copy()

        # ---------------------------------------------------------
        # Dynamic Left HUD: Status Badge, Elapsed Timer, FPS Metric
        # ---------------------------------------------------------
        badge_w, badge_h = 100, 28
        badge_x, badge_y = 16, 12

        if state == "RECORDING":
            # Pulsing red recording dot
            pulse = int(180 + 75 * math.sin(time.time() * 5.0))
            dot_color = (40, 40, pulse)
            cv2.rectangle(canvas, (badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h), (28, 20, 36), -1)
            cv2.rectangle(canvas, (badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h), dot_color, 1, cv2.LINE_AA)
            cv2.circle(canvas, (badge_x + 16, badge_y + 14), 5, ACCENT_RED, -1, cv2.LINE_AA)
            cv2.putText(canvas, "REC", (badge_x + 32, badge_y + 19), cv2.FONT_HERSHEY_SIMPLEX, 0.45, TEXT_WHITE, 1, cv2.LINE_AA)

        elif state == "PAUSED":
            cv2.rectangle(canvas, (badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h), (25, 35, 45), -1)
            cv2.rectangle(canvas, (badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h), ACCENT_AMBER, 1, cv2.LINE_AA)
            cv2.circle(canvas, (badge_x + 16, badge_y + 14), 5, ACCENT_AMBER, -1, cv2.LINE_AA)
            cv2.putText(canvas, "PAUSED", (badge_x + 28, badge_y + 19), cv2.FONT_HERSHEY_SIMPLEX, 0.42, ACCENT_AMBER, 1, cv2.LINE_AA)

        else:  # IDLE
            cv2.rectangle(canvas, (badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h), CARD_GRAY, -1)
            cv2.rectangle(canvas, (badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h), BORDER_LIGHT, 1, cv2.LINE_AA)
            cv2.circle(canvas, (badge_x + 16, badge_y + 14), 5, ACCENT_GREEN, -1, cv2.LINE_AA)
            cv2.putText(canvas, "READY", (badge_x + 30, badge_y + 19), cv2.FONT_HERSHEY_SIMPLEX, 0.45, TEXT_WHITE, 1, cv2.LINE_AA)

        # Elapsed Timer (00:00:00 when idle)
        hours = int(active_elapsed_sec // 3600)
        minutes = int((active_elapsed_sec % 3600) // 60)
        seconds = int(active_elapsed_sec % 60)
        timer_text = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        cv2.putText(canvas, timer_text, (badge_x + badge_w + 16, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.65, TEXT_WHITE, 2, cv2.LINE_AA)

        # Metrics on right of header
        src_h, src_w = raw_screen_frame.shape[:2]
        fps_display = f"FPS: {current_fps:.1f} / {self.target_fps}"
        cv2.putText(canvas, fps_display, (540, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT_WHITE, 1, cv2.LINE_AA)
        res_display = f"{src_w}x{src_h}"
        cv2.putText(canvas, res_display, (720, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.45, TEXT_GRAY, 1, cv2.LINE_AA)

        # ---------------------------------------------------------
        # Dynamic Scaled Preview Blit
        # ---------------------------------------------------------
        if (src_w, src_h) != self._cached_src_dims:
            preview_box_x = 16
            preview_box_y = 66
            preview_box_w = 860 - 32
            preview_box_h = 610

            scale = min(preview_box_w / src_w, preview_box_h / src_h)
            scaled_w = int(src_w * scale)
            scaled_h = int(src_h * scale)
            offset_x = preview_box_x + (preview_box_w - scaled_w) // 2
            offset_y = preview_box_y + (preview_box_h - scaled_h) // 2
            self._preview_scale_rect = (offset_x, offset_y, scaled_w, scaled_h)
            self._cached_src_dims = (src_w, src_h)

        ox, oy, sw, sh = self._preview_scale_rect

        # Fast blit into viewport slice
        resized = cv2.resize(raw_screen_frame, (sw, sh), interpolation=cv2.INTER_NEAREST)
        canvas[oy:oy + sh, ox:ox + sw] = resized

        if self.show_fps_on_preview:
            hud_tag = f"LIVE {current_fps:.1f} FPS"
            cv2.putText(canvas, hud_tag, (ox + 12, oy + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(canvas, hud_tag, (ox + 12, oy + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT_WHITE, 1, cv2.LINE_AA)

        # ---------------------------------------------------------
        # Dynamic Buffer Gauge Fill
        # ---------------------------------------------------------
        buf_ratio = min(1.0, buffer_len / max(1, buffer_max))
        gauge_y = 688
        gauge_w = 860 - 32
        gauge_h = 24

        if buf_ratio > 0:
            fill_w = int((gauge_w - 2) * buf_ratio)
            # Subtle gray-green bar
            bar_color = (60, 140, 70) if buf_ratio >= 0.99 else (90, 90, 90)
            cv2.rectangle(canvas, (17, gauge_y + 1), (17 + fill_w, gauge_y + gauge_h - 1), bar_color, -1)

        buffer_sec_filled = buf_ratio * self.buffer_seconds
        status_label = "READY TO CLIP" if buf_ratio >= 0.99 else f"BUFFERING ({int(buf_ratio * 100)}%)"
        gauge_text = f"Instant Buffer: {buffer_sec_filled:.1f}s / {self.buffer_seconds}s ({buffer_len}/{buffer_max} frames) - {status_label}"
        cv2.putText(canvas, gauge_text, (24, gauge_y + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.42, TEXT_WHITE, 1, cv2.LINE_AA)

        # ---------------------------------------------------------
        # Dynamic Toast Status Bar
        # ---------------------------------------------------------
        toast_y = 706
        toast_w = 500 - 32
        toast_h = 44
        cv2.rectangle(canvas, (860 + 16, toast_y), (860 + 16 + toast_w, toast_y + toast_h), CARD_GRAY, -1)
        cv2.rectangle(canvas, (860 + 16, toast_y), (860 + 16 + toast_w, toast_y + toast_h), BORDER_LIGHT, 1, cv2.LINE_AA)
        cv2.putText(canvas, self.toast_text, (860 + 28, toast_y + 27), cv2.FONT_HERSHEY_SIMPLEX, 0.42, self.toast_color, 1, cv2.LINE_AA)

        return canvas
