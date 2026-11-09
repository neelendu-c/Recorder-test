"""
Main application module coordinating the Screen Recorder Studio lifecycle.
Starts up in an interactive IDLE standby dashboard with a minimalist black & gray contrast UI,
allowing the user to manage settings/recordings and start/stop recording on demand.

Two threads:
- Capture (high priority): takes each frame from the active source, which paces it at the
  target frame rate (dxcam's high-resolution timer for displays, a deadline sleep for
  windows), and hands it to the recording and the replay buffer. Nothing else runs there.
- Main: hotkeys, the dashboard (render, imshow, waitKey) and every settings change, at the
  UI refresh rate. Drawing the dashboard takes several frame times at 240 FPS, so on the
  capture thread it would cost captured frames.
Changes that affect what the capture thread touches (source, canvas size, writers) briefly
pause it (_capture_paused), so a frame never reaches a writer of the wrong size.
"""
from contextlib import contextmanager
import ctypes
from pathlib import Path
import sys
import threading
import time
import cv2

import config
import win32_api
from audio_capture import AudioRouter
from hotkeys import HotkeyManager, display_name
from overlay import FPSCounter, LiveOverlay, OverlayStats
from replay_buffer import create_replay_buffer, remove_stale_ring_files, select_tier
from screen_capture import CaptureCompositor
from ui_dashboard import StudioDashboard, TEXT_GRAY, TEXT_WHITE, ACCENT_AMBER, ACCENT_RED
from video_writer import AsyncVideoWriter, generate_timestamped_filename, prepare_clip_encoder

# Short names for the overlay's hotkey lines
OVERLAY_HOTKEY_NAMES = {"record_toggle": "Rec", "pause": "Pause", "clip": "Clip", "lock_window": "Lock",
                        "mic_toggle": "Mic"}


class ScreenRecorderApp:
    """
    Orchestrates the Screen Recorder Studio lifecycle:
    - Starts in IDLE mode with live preview and settings/file manager.
    - Transitions to RECORDING on demand (via UI button or hotkey).
    - Stops recording back to IDLE without shutting down the application.
    """

    def __init__(self):
        config.load_user_settings()
        config.ensure_directories()
        remove_stale_ring_files()

        # Request 1ms timer precision from Windows kernel
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass

        # Target framerate and buffer settings
        self.target_fps = config.FPS_NOMINAL
        self.buffer_seconds = config.CLIP_DURATION_SECONDS

        self.compositor = CaptureCompositor(fps=self.target_fps)
        # Output resolution: frames are scaled once in the compositor, before every consumer.
        self.compositor.set_canvas_size(config.output_canvas_size(self.compositor.native_size))
        self.fps_counter = FPSCounter()

        # Picks the fastest video encoder in the background (once), so recording starts quickly.
        prepare_clip_encoder(self.compositor.canvas_size, self.target_fps)

        # Audio: desktop / per-app / microphone tracks, routed per Settings > Audio.
        self.audio = AudioRouter()
        self.audio.start()

        self.hotkeys = HotkeyManager(config.HOTKEYS, config.DEFAULT_HOTKEYS)

        # Studio UI Dashboard (Simple Black & Gray Contrast)
        self.ui = StudioDashboard(
            recordings_dir=config.RECORDINGS_DIR,
            clips_dir=config.CLIPS_DIR,
            initial_fps=self.target_fps,
            initial_buffer_seconds=self.buffer_seconds,
            burn_fps_initial=config.BURN_FPS_ON_RECORDING,
            show_fps_initial=config.SHOW_FPS_ON_PREVIEW,
            hotkeys=self.hotkeys,
        )
        self.ui.audio = self.audio
        self.ui.native_size = self.compositor.native_size
        self.ui.canvas_size = self.compositor.canvas_size

        # Lifecycle State: "IDLE", "RECORDING", "PAUSED"
        self.state = "IDLE"
        self.live_writer: AsyncVideoWriter = None
        self._encoder_warning_shown = False
        self.current_filepath: Path = None

        self.active_elapsed_time = 0.0
        self.segment_start_active_time = 0.0
        self.segment_index = 1
        self.frames_written = 0

        # Capture thread (started in run())
        self._capture_thread: threading.Thread = None
        self._capture_running = False
        self._latest_frame = None
        self._pause_requested = threading.Event()
        self._capture_idle = threading.Event()
        self._capture_resume = threading.Event()
        self._pause_depth = 0
        self._retired = []

        self.replay_buffer = None
        self._configure_replay()

        # Live overlay: nothing exists (no thread, no window) while it is disabled.
        self.overlay_stats = OverlayStats()
        self.overlay = None
        self._overlay_error = None
        self._overlay_hotkeys_version = -1
        if config.OVERLAY_ENABLED:
            self._start_overlay()

        self._actions = {
            "record_toggle": self.toggle_recording,
            "pause": self.toggle_pause,
            "clip": self.save_clip,
            "lock_window": self.toggle_window_lock,
            "mic_toggle": self.toggle_microphone,
        }
        self._sync_source_to_ui()

    # --- Recording ------------------------------------------------------------------

    def start_recording(self) -> None:
        """Transitions from IDLE to RECORDING mode."""
        if self.state != "IDLE":
            return

        width, height = self.compositor.canvas_size
        self.segment_index = 1
        live_filename = generate_timestamped_filename(
            f"screen_live_part{self.segment_index}", extension=config.VIDEO_EXTENSION
        )
        self.current_filepath = Path(config.RECORDINGS_DIR) / live_filename

        writer = AsyncVideoWriter(
            output_path=self.current_filepath,
            fourcc_str=config.FOURCC_CODEC,
            fps=self.target_fps,
            dimensions=(width, height),
            audio_source=self.audio,
        )

        self.active_elapsed_time = 0.0
        self.segment_start_active_time = 0.0
        self.frames_written = 0
        self._encoder_warning_shown = False
        # A minimized window can't be captured, so keep the captured window restored while recording.
        self.compositor.set_keep_window_restored(True)

        # The capture thread checks the state, then uses the writer, so the writer goes first.
        self.live_writer = writer
        self.state = "RECORDING"
        self.ui.state = "RECORDING"
        self.ui.set_toast(f"Recording started: {self.current_filepath.name}", TEXT_WHITE)
        print(f"Recording started at {self.target_fps} FPS: {self.current_filepath.name}")

    def stop_recording(self) -> None:
        """Transitions from RECORDING/PAUSED back to IDLE mode without exiting."""
        if self.state == "IDLE":
            return

        # Stop feeding the writer first; finishing the file (encode, mux) then happens
        # here on the main thread while capture carries on for the replay buffer.
        writer = self.live_writer
        self.state = "IDLE"
        self.live_writer = None
        if writer:
            writer.release()

        saved_name = self.current_filepath.name if self.current_filepath else "video"
        self.compositor.set_keep_window_restored(False)
        self.ui.state = "IDLE"
        self.active_elapsed_time = 0.0

        # Refresh file lists so newly completed recording appears in UI immediately
        self.ui.refresh_file_lists()
        self.ui.set_toast(f"Saved: {saved_name}. Ready for next recording.", TEXT_WHITE)
        print(f"Recording stopped and saved to: {saved_name}")

    def toggle_recording(self) -> None:
        if self.state == "IDLE":
            self.start_recording()
        else:
            self.stop_recording()

    def toggle_pause(self) -> None:
        if self.state == "RECORDING":
            self.state = "PAUSED"
            self.ui.state = "PAUSED"
            self.ui.set_toast("Recording paused.", ACCENT_AMBER)
        elif self.state == "PAUSED":
            self.state = "RECORDING"
            self.ui.state = "RECORDING"
            self.ui.set_toast("Recording resumed.", TEXT_WHITE)

    # --- Instant replay -------------------------------------------------------------

    def save_clip(self) -> None:
        """Exports the instant replay buffer (hotkey or UI button)."""
        replay = self.replay_buffer
        if replay is None:
            self.ui.set_toast("Instant replay is turned off in Settings.", ACCENT_AMBER)
            return

        if replay.is_full():
            def _on_clip_complete(saved_path: Path):
                self.ui.refresh_file_lists()
                self.ui.set_toast(f"Clip saved: {saved_path.name}", TEXT_WHITE)
                print(f"Instant replay clip saved: {saved_path.name}")

            replay.save_clip_async(
                output_dir=config.CLIPS_DIR,
                fps=self.target_fps,
                dimensions=self.compositor.canvas_size,
                fourcc_str=config.FOURCC_CODEC,
                prefix=f"clipped_{self.buffer_seconds}s",
                on_complete=_on_clip_complete,
            )
            self.ui.set_toast(f"Exporting {self.ui.duration_label(self.buffer_seconds)} clip to Clips/...", TEXT_WHITE)
        else:
            cur_frames = len(replay)
            max_frames = replay.max_frames
            pct = int((cur_frames / max(1, max_frames)) * 100)
            self.ui.set_toast(
                f"Buffer filling: {pct}% ({cur_frames}/{max_frames} frames). Wait until full.",
                ACCENT_AMBER,
            )

    def _configure_replay(self, announce: bool = False, force: bool = False) -> None:
        """Creates, resizes or removes the replay buffer to match the current settings.
        force: start a new buffer even if the tier and frame rate are unchanged."""
        if not config.REPLAY_ENABLED:
            if self.replay_buffer is not None:
                old, self.replay_buffer = self.replay_buffer, None
                self._retire(old)
            self.ui.replay_tier = "off"
            return

        dimensions = self.compositor.canvas_size
        tier = select_tier(dimensions, self.target_fps, self.buffer_seconds)
        current = self.replay_buffer
        if current is not None and current.tier == tier and not force and current.dimensions == dimensions:
            if tier == "ram":
                current.resize(int(self.target_fps * self.buffer_seconds), self.target_fps)
                return
            if current.fps == self.target_fps:
                current.set_duration(self.buffer_seconds)
                return

        self.replay_buffer = create_replay_buffer(
            tier, dimensions, self.target_fps, self.buffer_seconds, self.audio, config.FOURCC_CODEC
        )
        if current is not None:
            self._retire(current)
        self.ui.replay_tier = tier
        self.ui.dirty = True
        if announce and current is not None:
            where = "in RAM" if tier == "ram" else "on disk"
            self.ui.set_toast(f"Replay buffer restarted {where}; it needs to refill before clipping.", ACCENT_AMBER)

    # --- Settings changes -----------------------------------------------------------

    def apply_fps(self, new_fps: int) -> None:
        """Changes the capture frame rate, rolling over to a new recording segment if needed."""
        new_fps = config.clamp_fps(new_fps)
        if new_fps == self.target_fps:
            return
        self.target_fps = new_fps
        config.FPS_NOMINAL = new_fps
        config.save_user_settings()
        with self._capture_paused():
            self.compositor.set_fps(new_fps)
            self._roll_over_segment(f"{new_fps}fps")
            self._configure_replay(announce=True)
        self.ui.target_fps = new_fps
        self.ui.pending_fps = new_fps
        self.ui.refresh_file_lists()
        self.ui.set_toast(f"Target framerate set to {new_fps} FPS", TEXT_WHITE)

    def _roll_over_segment(self, tag: str) -> None:
        """While recording, closes the current file and continues in a new one with the new settings."""
        if self.state not in ("RECORDING", "PAUSED"):
            return
        old_writer = self.live_writer
        self.segment_index += 1
        new_filename = generate_timestamped_filename(
            f"screen_live_part{self.segment_index}_{tag}",
            extension=config.VIDEO_EXTENSION,
        )
        self.current_filepath = Path(config.RECORDINGS_DIR) / new_filename
        self.live_writer = AsyncVideoWriter(
            output_path=self.current_filepath,
            fourcc_str=config.FOURCC_CODEC,
            fps=self.target_fps,
            dimensions=self.compositor.canvas_size,
            audio_source=self.audio,
        )
        self.frames_written = 0
        self.segment_start_active_time = self.active_elapsed_time
        self._retire(old_writer)

    # --- Capture thread coordination ------------------------------------------------

    @contextmanager
    def _capture_paused(self):
        """
        Holds the capture thread between frames while the source, canvas size, writers or
        replay buffer change, so no frame reaches a consumer of the wrong size. The pause
        lasts as long as the swap itself; old writers and buffers are finished afterwards.
        """
        thread = self._capture_thread
        if self._pause_depth or thread is None or not thread.is_alive() or thread is threading.current_thread():
            self._pause_depth += 1
            try:
                yield
            finally:
                self._pause_depth -= 1
                if not self._pause_depth:
                    self._release_retired()
            return
        self._pause_depth += 1
        self._capture_resume.clear()
        self._pause_requested.set()
        # At most one frame interval (longer only if the source is restarting).
        self._capture_idle.wait(timeout=2.0)
        try:
            yield
        finally:
            self._pause_requested.clear()
            self._capture_idle.clear()
            self._capture_resume.set()
            self._pause_depth -= 1
            self._release_retired()

    def _retire(self, item) -> None:
        """Finishes an old writer or replay buffer once capture no longer uses it."""
        if item is None:
            return
        self._retired.append(item)
        if not self._pause_depth:
            self._release_retired()

    def _release_retired(self) -> None:
        retired, self._retired = self._retired, []
        for item in retired:
            try:
                if isinstance(item, AsyncVideoWriter):
                    item.release()
                else:
                    item.close()
            except Exception as e:
                print(f"Warning: could not finish {type(item).__name__}: {e}")

    def apply_video_settings(self, request: dict) -> None:
        """Applies a new output resolution and/or quality (Settings > Video > APPLY)."""
        resolution = request.get("resolution", config.OUTPUT_RESOLUTION)
        quality = request.get("quality", config.VIDEO_QUALITY)
        bitrate = config.clamp_bitrate(request.get("bitrate", config.VIDEO_BITRATE_KBPS))
        old_canvas = self.compositor.canvas_size
        old_encoding = (config.VIDEO_QUALITY, config.VIDEO_BITRATE_KBPS)

        config.OUTPUT_RESOLUTION = resolution
        config.VIDEO_QUALITY = quality
        config.VIDEO_BITRATE_KBPS = bitrate
        config.save_user_settings()

        new_canvas = config.output_canvas_size(self.compositor.native_size)
        canvas_changed = new_canvas != old_canvas
        encoding_changed = (quality, bitrate) != old_encoding
        self.ui.canvas_size = new_canvas
        if not canvas_changed and not encoding_changed:
            return

        # A file can't change resolution or encoder settings partway, so recordings continue in a
        # new segment, and the disk replay ring restarts (its segments must match to be joined).
        # Capture is held while the canvas and every consumer change together.
        with self._capture_paused():
            self.compositor.set_canvas_size(new_canvas)
            self._roll_over_segment(f"{new_canvas[1]}p_{quality}")
            self._configure_replay(announce=True, force=canvas_changed or (
                encoding_changed and self.replay_buffer is not None and self.replay_buffer.tier == "disk"))
        self.ui.refresh_file_lists()
        self.ui.set_toast(f"Video output: {new_canvas[0]}x{new_canvas[1]}, {self.ui.quality_label()}", TEXT_WHITE)

    # --- Audio ----------------------------------------------------------------------

    def toggle_microphone(self) -> None:
        config.AUDIO_MIC_ENABLED = not config.AUDIO_MIC_ENABLED
        config.save_user_settings()
        self.audio.apply()
        self.ui.dirty = True
        self.ui.set_toast(f"Microphone: {'ON' if config.AUDIO_MIC_ENABLED else 'OFF'}",
                          TEXT_WHITE if config.AUDIO_MIC_ENABLED else ACCENT_AMBER)

    def apply_buffer_duration(self, seconds: int) -> None:
        seconds = config.clamp_clip_duration(seconds)
        self.buffer_seconds = seconds
        config.CLIP_DURATION_SECONDS = seconds
        config.save_user_settings()
        self.ui.buffer_seconds = seconds
        self.ui.pending_buffer_seconds = seconds
        self._configure_replay(announce=True)
        self.ui.set_toast(f"Instant replay buffer set to {self.ui.duration_label(seconds)}", TEXT_WHITE)

    def toggle_replay(self) -> None:
        config.REPLAY_ENABLED = not config.REPLAY_ENABLED
        config.save_user_settings()
        self._configure_replay()
        status = "ON" if config.REPLAY_ENABLED else "OFF"
        self.ui.set_toast(f"Instant replay buffer: {status}", TEXT_WHITE)

    # --- Capture source -------------------------------------------------------------

    def _sync_source_to_ui(self) -> None:
        self.ui.source_label = self.compositor.label
        self.ui.source_key = self.compositor.source_key
        self.ui.source_hwnd = self.compositor.source_hwnd
        self.ui.monitor_device = self.compositor.monitor_device
        self.ui.source_minimized = self.compositor.window_minimized
        self.ui.dirty = True

    def select_source(self, request: dict) -> None:
        with self._capture_paused():
            if request["kind"] == "display":
                error = self.compositor.select_display(request["display"])
            else:
                error = self.compositor.select_window(request["hwnd"], request["title"])
        self._sync_source_to_ui()
        if error:
            self.ui.set_toast(error, ACCENT_RED)
        else:
            self.ui.set_toast(f"Capturing: {self.compositor.label}", TEXT_WHITE)

    def toggle_window_lock(self) -> None:
        """Locks capture onto the foreground window; pressing again returns to the display."""
        hwnd = win32_api.foreground_window()
        if self.compositor.kind == "window" and (hwnd is None or hwnd == self.compositor.source_hwnd):
            with self._capture_paused():
                self.compositor.restore_display()
            self._sync_source_to_ui()
            self.ui.set_toast(f"Window lock released. Capturing {self.compositor.label}.", TEXT_WHITE)
            return
        if hwnd is None:
            self.ui.set_toast("No capturable window is in the foreground.", ACCENT_AMBER)
            return
        title = win32_api.get_window_title(hwnd) or "Window"
        self.select_source({"kind": "window", "hwnd": hwnd, "title": title})

    # --- Live overlay ---------------------------------------------------------------

    def _start_overlay(self) -> None:
        self._overlay_hotkeys_version = -1
        self.overlay = LiveOverlay(self.overlay_stats, self._on_overlay_error)
        self._update_overlay_stats()
        self.overlay.start()

    def _on_overlay_error(self, message: str) -> None:
        # Called on the overlay thread; handled by the main loop.
        self._overlay_error = message

    def apply_overlay_settings(self) -> None:
        if config.OVERLAY_ENABLED and self.overlay is None:
            self._start_overlay()
        elif not config.OVERLAY_ENABLED and self.overlay is not None:
            self.overlay.stop()
            self.overlay = None
        elif self.overlay is not None:
            self.overlay.apply_settings(config.OVERLAY_STATS, config.OVERLAY_POSITION, config.OVERLAY_MONITOR,
                                        (config.OVERLAY_X, config.OVERLAY_Y))

    def _update_overlay_stats(self) -> None:
        stats = self.overlay_stats
        stats.fps = self.fps_counter.display_fps
        stats.target_fps = self.target_fps
        stats.state = self.state
        stats.elapsed = self.active_elapsed_time
        stats.source = self.compositor.label
        stats.source_hwnd = self.compositor.source_hwnd
        stats.monitor_device = self.compositor.monitor_device
        if self.hotkeys.version != self._overlay_hotkeys_version:
            self._overlay_hotkeys_version = self.hotkeys.version
            entries = [
                f"{display_name(combo)} {OVERLAY_HOTKEY_NAMES.get(action, action)}"
                for action, combo in self.hotkeys.bindings().items() if combo
            ]
            stats.hotkeys = tuple("   ".join(entries[i:i + 2]) for i in range(0, len(entries), 2))

    # --- UI requests ----------------------------------------------------------------

    def _process_ui_requests(self) -> bool:
        """Applies actions requested through the dashboard. Returns True to exit."""
        ui = self.ui
        if ui.request_start_recording:
            ui.request_start_recording = False
            self.start_recording()

        if ui.request_stop_recording:
            ui.request_stop_recording = False
            self.stop_recording()

        if ui.request_pause_toggle:
            ui.request_pause_toggle = False
            self.toggle_pause()

        if ui.request_clip:
            ui.request_clip = False
            self.save_clip()

        if ui.request_exit_app:
            print("Exit requested via Studio UI...")
            return True

        if ui.request_fps_change is not None:
            new_fps = ui.request_fps_change
            ui.request_fps_change = None
            self.apply_fps(new_fps)

        if ui.request_buffer_change is not None:
            new_duration = ui.request_buffer_change
            ui.request_buffer_change = None
            self.apply_buffer_duration(new_duration)

        if ui.request_replay_toggle:
            ui.request_replay_toggle = False
            self.toggle_replay()

        if ui.request_source is not None:
            request = ui.request_source
            ui.request_source = None
            self.select_source(request)

        if ui.request_overlay_update:
            ui.request_overlay_update = False
            self.apply_overlay_settings()

        if ui.request_video_change is not None:
            request = ui.request_video_change
            ui.request_video_change = None
            self.apply_video_settings(request)

        if ui.request_audio_update:
            ui.request_audio_update = False
            self.audio.apply()

        if ui.request_mic_toggle:
            ui.request_mic_toggle = False
            self.toggle_microphone()
        return False

    def _handle_background_notices(self) -> None:
        """Shows messages raised by the capture source, overlay, or replay buffer."""
        notice = self.compositor.take_notice()
        if notice:
            self._sync_source_to_ui()
            self.ui.set_toast(notice, ACCENT_AMBER)

        if self._overlay_error is not None:
            message, self._overlay_error = self._overlay_error, None
            if self.overlay is not None:
                self.overlay.stop()
                self.overlay = None
            config.OVERLAY_ENABLED = False
            config.save_user_settings()
            self.ui.dirty = True
            self.ui.set_toast(message, ACCENT_RED)

        writer = self.live_writer
        if writer is not None and not self._encoder_warning_shown and writer.dropped_frames >= self.target_fps:
            # Over a second of frames held over: the encoder can't sustain this rate. The file
            # still has the right length and sync, but repeats frames.
            self._encoder_warning_shown = True
            width, height = self.compositor.canvas_size
            self.ui.set_toast(f"The encoder can't keep up with {self.target_fps} FPS at {width}x{height}; frames are "
                              f"being repeated. Lower the FPS or resolution for smooth video.", ACCENT_AMBER)

        if self.replay_buffer is not None:
            warning = self.replay_buffer.take_warning()
            if warning:
                self.ui.set_toast(warning, ACCENT_RED)

    # --- Main loop ------------------------------------------------------------------

    # --- Capture thread -------------------------------------------------------------

    def _capture_loop(self) -> None:
        """Takes every frame from the source and hands it to the recording and the replay buffer."""
        win32_api.set_current_thread_priority(win32_api.THREAD_PRIORITY_ABOVE_NORMAL)
        last_time = time.perf_counter()
        while self._capture_running:
            if self._pause_requested.is_set():
                # Held between frames while the main thread swaps sources, canvas or writers.
                self._capture_idle.set()
                self._capture_resume.wait(timeout=5.0)
                last_time = time.perf_counter()
                continue
            try:
                # The source returns each new screen image (at most one per frame slot) with
                # the time it was on screen, or repeats the last one when nothing changed.
                frame, capture_time = self.compositor.next_frame()
            except Exception as e:
                print(f"Warning: capture error: {e}")
                time.sleep(0.05)
                continue
            now = time.perf_counter()
            dt = now - last_time
            last_time = now
            self.fps_counter.update()

            writer = self.live_writer
            if self.state == "RECORDING" and writer is not None:
                self.active_elapsed_time += dt
                if self.ui.burn_fps_on_video:
                    frame_to_write = frame.copy()
                    self.fps_counter.draw_fps(frame_to_write)
                else:
                    frame_to_write = frame
                # Placed by when it was on screen; the writer's timeline handles pacing.
                writer.write(frame_to_write, capture_time)
                self.frames_written += 1

            replay = self.replay_buffer
            if replay is not None:
                replay.add_frame(frame, capture_time)
            self._latest_frame = frame  # For the dashboard preview

    def _poll_hotkeys(self) -> None:
        for action in self.hotkeys.poll():
            handler = self._actions.get(action)
            if handler is not None:
                handler()

    # --- Main loop ------------------------------------------------------------------

    def run(self) -> None:
        """Runs the studio: capture on its own thread, hotkeys and dashboard here."""
        ui_interval = 1.0 / config.UI_REFRESH_HZ
        hotkey_interval = 0.005  # Hotkeys are checked every 5 ms between dashboard frames

        controls = " | ".join(
            f"[{display_name(combo)}] {config.HOTKEY_LABELS[action]}"
            for action, combo in self.hotkeys.bindings().items() if combo
        )
        print("Screen Recorder Studio initialized.")
        print(f"Controls: {controls} | [ESC] Exit (Studio window)")
        self.hotkeys.register()

        # Window setup
        if config.SHOW_PREVIEW:
            cv2.namedWindow(config.WINDOW_NAME, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(config.WINDOW_NAME, config.WINDOW_WIDTH, config.WINDOW_HEIGHT)
            cv2.setMouseCallback(config.WINDOW_NAME, self.ui.on_mouse)
            if config.START_MAXIMIZED:
                # The window only exists once something is shown in it.
                cv2.imshow(config.WINDOW_NAME, self.ui.base_canvas)
                cv2.waitKey(1)
                win32_api.maximize_window(config.WINDOW_NAME)

        # Python lets a thread keep the GIL for up to 5 ms before another may run, longer
        # than a 240 FPS frame. 1 ms keeps the capture thread from waiting on the dashboard.
        sys.setswitchinterval(0.001)
        self._capture_running = True
        self._capture_thread = threading.Thread(target=self._capture_loop, name="Capture", daemon=True)
        self._capture_thread.start()

        try:
            while True:
                tick_start = time.perf_counter()
                self._poll_hotkeys()
                if self._process_ui_requests():
                    break
                if self.overlay is not None:
                    self._update_overlay_stats()
                self._handle_background_notices()

                minimized = self.compositor.window_minimized
                if minimized != self.ui.source_minimized:
                    self.ui.source_minimized = minimized
                    self.ui.dirty = True

                if config.SHOW_PREVIEW:
                    frame = self._latest_frame
                    if frame is not None:
                        # Render at the window's own size so the image is never stretched.
                        try:
                            _, _, win_w, win_h = cv2.getWindowImageRect(config.WINDOW_NAME)
                            self.ui.set_window_size(win_w, win_h)
                        except cv2.error:
                            pass
                        replay = self.replay_buffer
                        dashboard_img = self.ui.render(
                            raw_screen_frame=frame,
                            current_fps=self.fps_counter.display_fps,
                            active_elapsed_sec=self.active_elapsed_time,
                            buffer_len=len(replay) if replay is not None else 0,
                            buffer_max=replay.max_frames if replay is not None else 0,
                            state=self.state,
                        )
                        cv2.imshow(config.WINDOW_NAME, dashboard_img)

                    # Check if user closed the window via title bar [X]
                    if cv2.getWindowProperty(config.WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                        print("Window closed by user...")
                        break

                    # Window-local keys (global hotkeys work everywhere, including here)
                    key = cv2.waitKey(1) & 0xFF
                    if key == config.WINDOW_EXIT_KEY:
                        print("Exit requested via ESC key...")
                        break

                # Wait for the next dashboard frame, still answering hotkeys promptly.
                next_tick = tick_start + ui_interval
                while True:
                    remaining = next_tick - time.perf_counter()
                    if remaining <= 0:
                        break
                    time.sleep(min(hotkey_interval, remaining))
                    self._poll_hotkeys()

        finally:
            self._capture_running = False
            self._capture_resume.set()
            if self._capture_thread is not None:
                self._capture_thread.join(timeout=3.0)
            if self.live_writer:
                self.live_writer.release()
            if self.replay_buffer is not None:
                self.replay_buffer.close()
            self._release_retired()
            if self.overlay is not None:
                self.overlay.stop()
            self.compositor.close()
            self.audio.stop()
            cv2.destroyAllWindows()
            self.hotkeys.unregister()
            try:
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass
            print("Screen Recorder Studio closed cleanly.")


def main() -> None:
    """Application entry point."""
    app = ScreenRecorderApp()
    app.run()


if __name__ == "__main__":
    main()
