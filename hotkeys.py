"""
Hotkey management module for user-configurable global keyboard shortcuts.

The keyboard library runs one background hook thread. Each hotkey callback only records
which action fired, and the main loop collects them with poll(), so the per-keystroke
cost is a few microseconds. Unlike RegisterHotKey, unmodified keys such as 'p' still
reach other applications.
"""
import threading
from typing import Callable, Dict, Optional, Set

import keyboard


# Media, volume and browser keys. Windows sometimes reports one of these (most often
# "play/pause media") alongside a real key press, e.g. from a keyboard's Fn/media layer,
# a headset, or a scan code shared with a normal key. They're never meant as part of a
# combination, so they're dropped from captured and saved bindings.
_MEDIA_KEY_WORDS = ("media", "track", "volume", "browser", "start mail", "start application")


def is_media_key(name: str) -> bool:
    name = name.strip().lower()
    return any(word in name for word in _MEDIA_KEY_WORDS)


def strip_media_keys(combo: str) -> str:
    """Removes media keys from a combination, unless the combination is only media keys."""
    combo = combo or ""
    parts = combo.split("+")
    if not any(is_media_key(part) for part in parts):
        return combo  # Unchanged, including combinations that use the '+' key itself
    kept = [part for part in parts if part.strip() and not is_media_key(part)]
    return "+".join(kept) if kept else combo


def display_name(combo: str) -> str:
    """Formats a combination for the UI, e.g. 'ctrl+shift+r' -> 'Ctrl+Shift+R'."""
    if not combo:
        return "Unbound"
    return "+".join(part.upper() if len(part) <= 3 else part.capitalize() for part in combo.split("+"))


class HotkeyManager:
    """Maps actions to rebindable global hotkeys."""

    def __init__(self, bindings: Dict[str, str], defaults: Dict[str, str]):
        self._bindings: Dict[str, str] = {}
        self._handles: Dict[str, object] = {}
        self._fired: Set[str] = set()
        self._fired_lock = threading.Lock()
        self._registered = False
        self.capturing = False
        self.version = 0  # Increments whenever a binding changes

        for action, default in defaults.items():
            combo = self.normalize(bindings.get(action, default))
            if not self.is_valid(combo):
                print(f"Warning: invalid hotkey '{combo}' for {action}. Using '{default}'.")
                combo = default
            self._bindings[action] = combo

    # --- Bindings -------------------------------------------------------------------

    @staticmethod
    def normalize(combo: str) -> str:
        return strip_media_keys((combo or "").strip().lower())

    @staticmethod
    def is_valid(combo: str) -> bool:
        if combo == "":
            return True
        try:
            keyboard.parse_hotkey(combo)
            return True
        except (ValueError, KeyError, TypeError):
            return False

    def bindings(self) -> Dict[str, str]:
        return dict(self._bindings)

    def binding(self, action: str) -> str:
        return self._bindings.get(action, "")

    def conflict_for(self, action: str, combo: str) -> Optional[str]:
        """Returns the other action already using this combination, if any."""
        if not combo:
            return None
        parsed = keyboard.parse_hotkey(combo)
        for other, other_combo in self._bindings.items():
            if other != action and other_combo and keyboard.parse_hotkey(other_combo) == parsed:
                return other
        return None

    def rebind(self, action: str, combo: str) -> Optional[str]:
        """Changes a binding. Returns an error message, or None on success."""
        combo = self.normalize(combo)
        if action not in self._bindings:
            return f"Unknown action: {action}"
        if not self.is_valid(combo):
            return f"'{combo}' isn't a valid key combination."
        conflict = self.conflict_for(action, combo)
        if conflict:
            return f"{display_name(combo)} is already used by '{conflict}'."

        self._remove(action)
        self._bindings[action] = combo
        if self._registered:
            self._add(action)
        self.version += 1
        return None

    # --- Registration ---------------------------------------------------------------

    def register(self) -> None:
        """Registers every bound hotkey with the system keyboard hook."""
        if not self._registered:
            for action in self._bindings:
                self._add(action)
            self._registered = True

    def unregister(self) -> None:
        """Removes every registered hotkey."""
        for action in list(self._handles):
            self._remove(action)
        self._registered = False

    def _add(self, action: str) -> None:
        combo = self._bindings[action]
        if not combo:
            return
        try:
            self._handles[action] = keyboard.add_hotkey(combo, self._fire, args=(action,))
        except Exception as e:
            print(f"Warning: could not register hotkey {combo} for {action}: {e}")

    def _remove(self, action: str) -> None:
        handle = self._handles.pop(action, None)
        if handle is not None:
            try:
                keyboard.remove_hotkey(handle)
            except Exception:
                pass

    def _fire(self, action: str) -> None:
        # Runs on the keyboard hook thread: record the action and return immediately.
        with self._fired_lock:
            self._fired.add(action)

    def poll(self) -> Set[str]:
        """Returns the actions fired since the last poll."""
        if not self._fired:  # Fast path for the common case, without taking the lock
            return set()
        with self._fired_lock:
            fired, self._fired = self._fired, set()
        return fired

    # --- Capturing a new combination ------------------------------------------------

    def start_capture(self, on_captured: Callable[[Optional[str]], None]) -> bool:
        """
        Waits on a background thread for the user to press a key combination and passes it
        to on_captured (None if they pressed Esc). Hotkeys are paused while waiting, so the
        keys being bound don't also trigger their current actions.
        """
        if self.capturing:
            return False
        self.capturing = True
        was_registered = self._registered
        self.unregister()

        def _worker():
            combo = None
            try:
                pressed = self.normalize(keyboard.read_hotkey(suppress=False))
                combo = None if pressed == "esc" else pressed
            except Exception as e:
                print(f"Warning: could not read hotkey: {e}")
            finally:
                if was_registered:
                    self.register()
                self.capturing = False
            on_captured(combo)

        threading.Thread(target=_worker, daemon=True, name="HotkeyCapture").start()
        return True

    def __enter__(self):
        self.register()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.unregister()
