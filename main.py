"""
Main application module coordinating the Screen Recorder Studio lifecycle.
Starts up in an interactive IDLE standby dashboard with a minimalist black & gray contrast UI,
allowing the user to manage settings/recordings and start/stop recording on demand.
"""
import ctypes
from pathlib import Path
import time
import cv2

import config
from hotkeys import HotkeyManager
from overlay import FPSCounter
from replay_buffer import ReplayBuffer
from screen_capture import ScreenCapture
from ui_dashboard import StudioDashboard, TEXT_GRAY, TEXT_WHITE, ACCENT_AMBER, ACCENT_RED
from video_writer import AsyncVideoWriter, generate_timestamped_filename


class ScreenRecorderApp:
    """
    Orchestrates the Screen Recorder Studio lifecycle:
    - Starts in IDLE mode with live preview and settings/file manager.
    - Transitions to RECORDING on demand (via UI button or F9 hotkey).
    - Stops recording back to IDLE without shutting down the application.
    """

    def __init__(self):
        config.ensure_directories()

        # Request 1ms timer precision from Windows kernel
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass

        self.capture = ScreenCapture(monitor_index=config.MONITOR_INDEX)
        self.fps_counter = FPSCounter()

        # Target framerate and buffer settings
        self.target_fps = config.FPS_NOMINAL
        self.buffer_seconds = config.CLIP_DURATION_SECONDS
        self.replay_buffer = ReplayBuffer(max_frames=self.target_fps * self.buffer_seconds)

        self.hotkeys = HotkeyManager(
            record_toggle_key=config.HOTKEY_RECORD_TOGGLE,
            clip_key=config.HOTKEY_CLIP,
            pause_key=config.HOTKEY_PAUSE,
        )

        # Studio UI Dashboard (Simple Black & Gray Contrast)
        self.ui = StudioDashboard(
            recordings_dir=config.RECORDINGS_DIR,
            clips_dir=config.CLIPS_DIR,
            initial_fps=self.target_fps,
            initial_buffer_seconds=self.buffer_seconds,
            burn_fps_initial=config.BURN_FPS_ON_RECORDING,
        )

        # Lifecycle State: "IDLE", "RECORDING", "PAUSED"
        self.state = "IDLE"
        self.live_writer: AsyncVideoWriter = None
        self.current_filepath: Path = None

        self.active_elapsed_time = 0.0
        self.segment_start_active_time = 0.0
        self.segment_index = 1
        self.frames_written = 0

    def start_recording(self) -> None:
        """Transitions from IDLE to RECORDING mode."""
        if self.state != "IDLE":
            return

        width, height = self.capture.dimensions
        self.segment_index = 1
        live_filename = generate_timestamped_filename(
            f"screen_live_part{self.segment_index}", extension=config.VIDEO_EXTENSION
        )
        self.current_filepath = Path(config.RECORDINGS_DIR) / live_filename

        self.live_writer = AsyncVideoWriter(
            output_path=self.current_filepath,
            fourcc_str=config.FOURCC_CODEC,
            fps=self.target_fps,
            dimensions=(width, height),
        )

        self.active_elapsed_time = 0.0
        self.segment_start_active_time = 0.0
        self.frames_written = 0

        self.state = "RECORDING"
        self.ui.state = "RECORDING"
        self.ui.set_toast(f"Recording started: {self.current_filepath.name}", TEXT_WHITE)
        print(f"Recording started at {self.target_fps} FPS: {self.current_filepath.name}")

    def stop_recording(self) -> None:
        """Transitions from RECORDING/PAUSED back to IDLE mode without exiting."""
        if self.state == "IDLE":
            return

        if self.live_writer:
            self.live_writer.release()
            self.live_writer = None

        saved_name = self.current_filepath.name if self.current_filepath else "video"
        self.state = "IDLE"
        self.ui.state = "IDLE"
        self.active_elapsed_time = 0.0

        # Refresh file lists so newly completed recording appears in UI immediately
        self.ui.refresh_file_lists()
        self.ui.set_toast(f"Saved: {saved_name}. Ready for next recording.", TEXT_WHITE)
        print(f"Recording stopped and saved to: {saved_name}")

    def run(self) -> None:
        """Runs the main studio loop."""
        width, height = self.capture.dimensions
        frame_interval = 1.0 / self.target_fps

        print("Screen Recorder Studio initialized.")
        print(f"Controls: [{config.HOTKEY_RECORD_TOGGLE.upper()}] Start/Stop Recording | [{config.HOTKEY_CLIP}] Clip | [ESC] Exit")
        self.hotkeys.register()

        # Window setup
        if config.SHOW_PREVIEW:
            cv2.namedWindow(config.WINDOW_NAME, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(config.WINDOW_NAME, config.WINDOW_WIDTH, config.WINDOW_HEIGHT)
            cv2.setMouseCallback(config.WINDOW_NAME, self.ui.on_mouse)

        last_loop_time = time.perf_counter()

        try:
            while True:
                loop_start = time.perf_counter()
                dt = loop_start - last_loop_time
                last_loop_time = loop_start

                # 1. Process Global Hotkeys
                if self.hotkeys.should_toggle_record():
                    self.hotkeys.reset_record_toggle()
                    if self.state == "IDLE":
                        self.start_recording()
                    else:
                        self.stop_recording()

                if self.hotkeys.should_pause():
                    self.hotkeys.reset_pause()
                    if self.state == "RECORDING":
                        self.state = "PAUSED"
                        self.ui.state = "PAUSED"
                        self.ui.set_toast("Recording paused.", ACCENT_AMBER)
                    elif self.state == "PAUSED":
                        self.state = "RECORDING"
                        self.ui.state = "RECORDING"
                        self.ui.set_toast("Recording resumed.", TEXT_WHITE)

                # 2. Process UI-driven Actions
                if self.ui.request_start_recording:
                    self.ui.request_start_recording = False
                    self.start_recording()

                if self.ui.request_stop_recording:
                    self.ui.request_stop_recording = False
                    self.stop_recording()

                if self.ui.request_pause_toggle:
                    self.ui.request_pause_toggle = False
                    if self.state == "RECORDING":
                        self.state = "PAUSED"
                        self.ui.state = "PAUSED"
                        self.ui.set_toast("Recording paused.", ACCENT_AMBER)
                    elif self.state == "PAUSED":
                        self.state = "RECORDING"
                        self.ui.state = "RECORDING"
                        self.ui.set_toast("Recording resumed.", TEXT_WHITE)

                if self.ui.request_exit_app:
                    print("Exit requested via Studio UI...")
                    break

                # 3. Handle Dynamic Framerate Switch
                if self.ui.request_fps_change is not None:
                    new_fps = self.ui.request_fps_change
                    self.ui.request_fps_change = None
                    if new_fps != self.target_fps:
                        self.target_fps = new_fps
                        frame_interval = 1.0 / self.target_fps

                        # If currently recording, roll over segment seamlessly
                        if self.state in ("RECORDING", "PAUSED"):
                            self.live_writer.release()
                            self.segment_index += 1
                            new_filename = generate_timestamped_filename(
                                f"screen_live_part{self.segment_index}_{new_fps}fps",
                                extension=config.VIDEO_EXTENSION,
                            )
                            self.current_filepath = Path(config.RECORDINGS_DIR) / new_filename
                            self.live_writer = AsyncVideoWriter(
                                output_path=self.current_filepath,
                                fourcc_str=config.FOURCC_CODEC,
                                fps=self.target_fps,
                                dimensions=(width, height),
                            )
                            self.frames_written = 0
                            self.segment_start_active_time = self.active_elapsed_time

                        new_max_frames = int(self.target_fps * self.ui.buffer_seconds)
                        self.replay_buffer.resize(new_max_frames)
                        self.ui.refresh_file_lists()

                # 4. Handle Dynamic Buffer Duration Switch
                if self.ui.request_buffer_change is not None:
                    new_duration = self.ui.request_buffer_change
                    self.ui.request_buffer_change = None
                    self.buffer_seconds = new_duration
                    new_max_frames = int(self.target_fps * new_duration)
                    self.replay_buffer.resize(new_max_frames)

                # 5. Capture Raw Screen Frame (Zero-copy memory slice)
                raw_frame = self.capture.grab_frame()

                # 6. Update Dynamic FPS Metrics
                self.fps_counter.update()
                display_fps = self.fps_counter.display_fps

                # 7. Record Frame (ONLY when actively recording)
                if self.state == "RECORDING":
                    self.active_elapsed_time += dt

                    if self.ui.burn_fps_on_video:
                        frame_to_write = raw_frame.copy()
                        self.fps_counter.draw_fps(frame_to_write)
                    else:
                        frame_to_write = raw_frame

                    # Wall-clock synchronization
                    segment_elapsed = self.active_elapsed_time - self.segment_start_active_time
                    target_frame_count = int(segment_elapsed * self.target_fps)
                    frames_to_write = target_frame_count - self.frames_written

                    if frames_to_write > 0:
                        clamped_writes = min(frames_to_write, 2)
                        for _ in range(clamped_writes):
                            self.live_writer.write(frame_to_write)
                        self.frames_written += frames_to_write

                # Replay buffer collects frames continuously
                self.replay_buffer.add_frame(raw_frame)

                # 8. Handle Instant Replay Clip Request (Hotkey or UI button)
                trigger_clip = False
                if self.hotkeys.should_clip():
                    self.hotkeys.reset_clip()
                    trigger_clip = True

                if self.ui.request_clip:
                    self.ui.request_clip = False
                    trigger_clip = True

                if trigger_clip:
                    if self.replay_buffer.is_full():
                        def _on_clip_complete(saved_path: Path):
                            self.ui.refresh_file_lists()
                            self.ui.set_toast(f"Clip saved: {saved_path.name}", TEXT_WHITE)
                            print(f"Instant replay clip saved: {saved_path.name}")

                        self.replay_buffer.save_clip_async(
                            output_dir=config.CLIPS_DIR,
                            fps=self.target_fps,
                            dimensions=(width, height),
                            fourcc_str=config.FOURCC_CODEC,
                            prefix=f"clipped_{self.ui.buffer_seconds}s",
                            on_complete=_on_clip_complete,
                        )
                        self.ui.set_toast(f"Exporting {self.ui.buffer_seconds}s clip to Clips/...", TEXT_WHITE)
                    else:
                        cur_frames = len(self.replay_buffer)
                        max_frames = self.replay_buffer.max_frames
                        pct = int((cur_frames / max(1, max_frames)) * 100)
                        self.ui.set_toast(
                            f"Buffer filling: {pct}% ({cur_frames}/{max_frames} frames). Wait until full.",
                            ACCENT_AMBER,
                        )

                # 9. Render Studio Dashboard Window (< 4ms)
                if config.SHOW_PREVIEW:
                    dashboard_img = self.ui.render(
                        raw_screen_frame=raw_frame,
                        current_fps=display_fps,
                        active_elapsed_sec=self.active_elapsed_time,
                        buffer_len=len(self.replay_buffer),
                        buffer_max=self.replay_buffer.max_frames,
                        state=self.state,
                    )
                    cv2.imshow(config.WINDOW_NAME, dashboard_img)

                    # Check if user closed the window via title bar [X]
                    if cv2.getWindowProperty(config.WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                        print("Window closed by user...")
                        break

                # 10. Process Keyboard Shortcuts in Window
                key = cv2.waitKey(1) & 0xFF
                if key == 27:  # ESC to exit application
                    print("Exit requested via ESC key...")
                    break
                elif key in (ord('r'), ord('R')):  # R to toggle recording
                    if self.state == "IDLE":
                        self.start_recording()
                    else:
                        self.stop_recording()
                elif key in (ord('p'), ord('P'), ord(' ')):  # P or Space to toggle pause
                    if self.state == "RECORDING":
                        self.state = "PAUSED"
                        self.ui.state = "PAUSED"
                        self.ui.set_toast("Recording paused.", ACCENT_AMBER)
                    elif self.state == "PAUSED":
                        self.state = "RECORDING"
                        self.ui.state = "RECORDING"
                        self.ui.set_toast("Recording resumed.", TEXT_WHITE)

                # 11. Frame Pacing to Maintain Target Framerate
                time_spent = time.perf_counter() - loop_start
                sleep_remaining = frame_interval - time_spent
                if sleep_remaining > 0.001:
                    time.sleep(sleep_remaining)

        finally:
            if self.live_writer:
                self.live_writer.release()
            cv2.destroyAllWindows()
            self.hotkeys.unregister()
            self.capture.close()
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
