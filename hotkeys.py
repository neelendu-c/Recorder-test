"""
Hotkey management module for capturing user keyboard shortcuts.
Supports global hotkeys for toggling recording, pausing, and instant replay clipping.
"""
import threading
import keyboard


class HotkeyManager:
    """Manages global keyboard hotkeys with thread-safe event signaling."""

    def __init__(
        self,
        record_toggle_key: str = "f9",
        clip_key: str = "~",
        pause_key: str = "p",
    ):
        self.record_toggle_key = record_toggle_key
        self.clip_key = clip_key
        self.pause_key = pause_key

        self._record_toggle_event = threading.Event()
        self._clip_event = threading.Event()
        self._pause_event = threading.Event()
        self._registered = False

    def register(self) -> None:
        """Registers global system keyboard hotkeys."""
        if not self._registered:
            try:
                keyboard.add_hotkey(self.record_toggle_key, self._trigger_record_toggle)
            except Exception as e:
                print(f"Warning: could not register hotkey {self.record_toggle_key}: {e}")

            try:
                keyboard.add_hotkey(self.clip_key, self._trigger_clip)
            except Exception as e:
                print(f"Warning: could not register hotkey {self.clip_key}: {e}")

            try:
                keyboard.add_hotkey(self.pause_key, self._trigger_pause)
            except Exception as e:
                print(f"Warning: could not register hotkey {self.pause_key}: {e}")

            self._registered = True

    def unregister(self) -> None:
        """Removes registered keyboard hotkeys safely."""
        if self._registered:
            for key in (self.record_toggle_key, self.clip_key, self.pause_key):
                try:
                    keyboard.remove_hotkey(key)
                except Exception:
                    pass
            self._registered = False

    def _trigger_record_toggle(self) -> None:
        self._record_toggle_event.set()

    def _trigger_clip(self) -> None:
        self._clip_event.set()

    def _trigger_pause(self) -> None:
        self._pause_event.set()

    def should_toggle_record(self) -> bool:
        """Returns True if the record toggle hotkey was pressed."""
        return self._record_toggle_event.is_set()

    def reset_record_toggle(self) -> None:
        self._record_toggle_event.clear()

    def should_clip(self) -> bool:
        """Returns True if the clip hotkey was pressed."""
        return self._clip_event.is_set()

    def reset_clip(self) -> None:
        self._clip_event.clear()

    def should_pause(self) -> bool:
        """Returns True if the pause hotkey was pressed."""
        return self._pause_event.is_set()

    def reset_pause(self) -> None:
        self._pause_event.clear()

    def __enter__(self):
        self.register()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.unregister()
