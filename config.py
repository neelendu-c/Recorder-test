"""
Configuration settings for the screen recorder application.

The values below are defaults. Settings changed in the Studio UI are saved to
user_settings.json and applied over these defaults by load_user_settings() at startup.
"""
import json
import os
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent
RECORDINGS_DIR = BASE_DIR / "Recordings"
CLIPS_DIR = BASE_DIR / "Clips"
TEMP_DIR = BASE_DIR / "Temp"  # In-progress encodes; kept apart so the UI never lists partial files
REPLAY_RING_DIR = TEMP_DIR / "replay_ring"  # Disk-tier replay segments
USER_SETTINGS_PATH = BASE_DIR / "user_settings.json"

# Video capture settings (wall-clock synchronized)
FPS_NOMINAL = 30
FPS_MIN = 15
FPS_MAX = 240
CLIP_DURATION_SECONDS = 30
CLIP_DURATION_MIN_SECONDS = 5
CLIP_DURATION_MAX_SECONDS = 3600
BUFFER_SIZE = FPS_NOMINAL * CLIP_DURATION_SECONDS
MONITOR_INDEX = 1

# Capture source and output canvas
CANVAS_SIZE = None  # Fixed (width, height) for every recording and clip; overrides OUTPUT_RESOLUTION
# Output resolution (Settings > Video). Frames are scaled once, right after capture, so the
# encoder, the replay buffer and the preview all work on the smaller image. Never upscales.
OUTPUT_RESOLUTION = "native"
OUTPUT_RESOLUTIONS = {"native": None, "2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480}
# Scaling filter: "linear" (cv2.INTER_LINEAR, fastest) or "area" (cv2.INTER_AREA, smoother downscales)
SCALE_INTERPOLATION = "linear"

# Video quality (Settings > Video): bitrate presets in Mbps for 1080p at 30 FPS. The actual
# bitrate scales with the output resolution and frame rate. "custom" uses VIDEO_BITRATE_KBPS.
VIDEO_QUALITY = "high"
VIDEO_QUALITY_PRESETS = {"low": 4, "medium": 8, "high": 16, "ultra": 32}
VIDEO_QUALITIES = ("low", "medium", "high", "ultra", "custom")
VIDEO_BITRATE_KBPS = 20000
VIDEO_BITRATE_MIN_KBPS = 1000
VIDEO_BITRATE_MAX_KBPS = 150000
# Windows doesn't draw minimized windows, so their capture feed freezes. These restore the
# captured window (without taking focus) so the feed keeps updating.
RESTORE_MINIMIZED_WINDOW_ON_SELECT = True
KEEP_CAPTURED_WINDOW_RESTORED_WHILE_RECORDING = True

# Video output settings (mp4v is natively supported by OpenCV MP4 container)
FOURCC_CODEC = "mp4v"
VIDEO_EXTENSION = ".mp4"

# Instant replay buffer
REPLAY_ENABLED = True
REPLAY_RAM_BUDGET_MB = None  # Raw frames are kept in RAM up to this size; None = 25% of installed RAM
# Disk tier: length of each rolling segment file. An export waits for the current segment
# to close and re-encodes the partial segments at each end, so shorter = faster exports.
REPLAY_SEGMENT_SECONDS = 2
# Video encoder for recordings, replay segments and clips: "auto" (each H.264 encoder, GPU and
# x264, is speed-tested at the current settings and the first that keeps up is used; results
# are cached in Temp/encoder_speeds.json for a day), "x264", or "opencv" (single-threaded
# mp4v, which ignores the quality setting)
CLIP_ENCODER = "auto"

# Audio sources (Settings > Audio). Each source is captured as its own track.
AUDIO_ENABLED = True
AUDIO_MIC_ENABLED = False          # Default Windows recording device
# Desktop audio: "all" = everything playing (minus AUDIO_BLOCKED_APPS),
# "selected" = only AUDIO_INCLUDED_APPS (per-app capture by process ID), "off" = none
AUDIO_DESKTOP_MODE = "all"
AUDIO_DESKTOP_MODES = ("all", "selected", "off")
AUDIO_INCLUDED_APPS = []           # Lower-case exe names, e.g. ["spotify.exe"]
AUDIO_BLOCKED_APPS = []
# "mixed" = one audio stream with every source summed; "separate" = that mixed stream plus
# one stream per source (desktop, each app, microphone) for editing
AUDIO_TRACK_LAYOUT = "mixed"
AUDIO_TRACK_LAYOUTS = ("mixed", "separate")
AUDIO_DEVICE = None  # PyAudio fallback only: None = default output device, or a substring of its name
AUDIO_CHUNK_FRAMES = 960  # PyAudio fallback capture block size (20 ms at 48 kHz)

# Audio format. Native capture asks Windows for exactly this; ffmpeg encodes AAC from it.
AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_BITRATE = "192k"

# Audio/video sync
AUDIO_SYNC_OFFSET_MS = 0  # Manual calibration; positive values move audio later
AUDIO_RESYNC_TOLERANCE_S = 0.04  # Audio timing error allowed before it is corrected
AV_GAP_CUT_SECONDS = 0.5  # Longer gaps between frames (pause, stall) are cut from video and audio

# Overlays and Watermarks
BURN_FPS_ON_RECORDING = False
SHOW_FPS_ON_PREVIEW = True

# Live on-screen overlay (excluded from capture, so it never appears in recordings)
OVERLAY_ENABLED = False
OVERLAY_STATS = {"fps": True, "source": True, "elapsed": True, "hotkeys": True}
OVERLAY_POSITION = "top_left"  # top_left, top_right, bottom_left, bottom_right, custom
# Used when OVERLAY_POSITION is "custom": pixels from the monitor's top-left corner.
# Set by dragging the overlay preview in Settings > Overlay.
OVERLAY_X = 16
OVERLAY_Y = 16
OVERLAY_MONITOR = "source"  # "source" = monitor being captured, "primary" = primary monitor
OVERLAY_REFRESH_HZ = 4
OVERLAY_POSITIONS = ("top_left", "top_right", "bottom_left", "bottom_right", "custom")
OVERLAY_MONITORS = ("source", "primary")

# Controls / Global Hotkeys (action -> key combination; "" = unbound)
DEFAULT_HOTKEYS = {
    "record_toggle": "f9",
    "pause": "p",
    "clip": "~",
    "lock_window": "f10",
    "mic_toggle": "f8",
}
HOTKEY_LABELS = {
    "record_toggle": "Start / Stop Recording",
    "pause": "Pause / Resume",
    "clip": "Save Instant Replay Clip",
    "lock_window": "Lock onto Foreground Window",
    "mic_toggle": "Microphone On / Off",
}
HOTKEYS = dict(DEFAULT_HOTKEYS)
WINDOW_EXIT_KEY = 27  # Esc; only works while the Studio window has focus

# Display settings (Simple Black & Gray Contrast Studio Window)
SHOW_PREVIEW = True
WINDOW_NAME = "Screen Recorder Studio"
WINDOW_WIDTH = 1360  # Size when not maximized; the layout adapts to any size you drag it to
WINDOW_HEIGHT = 768
START_MAXIMIZED = True
UI_REFRESH_HZ = 30  # Dashboard redraw rate, independent of the capture frame rate

# Settings the UI can change and that are saved to user_settings.json
PERSISTED_SETTINGS = (
    "FPS_NOMINAL",
    "CLIP_DURATION_SECONDS",
    "REPLAY_ENABLED",
    "BURN_FPS_ON_RECORDING",
    "SHOW_FPS_ON_PREVIEW",
    "OVERLAY_ENABLED",
    "OVERLAY_STATS",
    "OVERLAY_POSITION",
    "OVERLAY_X",
    "OVERLAY_Y",
    "OVERLAY_MONITOR",
    "HOTKEYS",
    "OUTPUT_RESOLUTION",
    "VIDEO_QUALITY",
    "VIDEO_BITRATE_KBPS",
    "AUDIO_MIC_ENABLED",
    "AUDIO_DESKTOP_MODE",
    "AUDIO_INCLUDED_APPS",
    "AUDIO_BLOCKED_APPS",
    "AUDIO_TRACK_LAYOUT",
)


def clamp_bitrate(value) -> int:
    return max(VIDEO_BITRATE_MIN_KBPS, min(VIDEO_BITRATE_MAX_KBPS, int(value)))


def output_canvas_size(native_size, resolution=None):
    """(width, height) recordings use for a display of native_size, per OUTPUT_RESOLUTION
    (or the given resolution key)."""
    if CANVAS_SIZE:
        return tuple(CANVAS_SIZE)
    width, height = native_size
    target_h = OUTPUT_RESOLUTIONS.get(resolution or OUTPUT_RESOLUTION)
    if target_h is None or target_h >= height:
        return width, height  # Native, or a preset at/above it (never upscale)
    target_w = int(round(width * target_h / height / 2)) * 2  # Even, as H.264 needs
    return target_w, target_h


def clamp_fps(value) -> int:
    return max(FPS_MIN, min(FPS_MAX, int(value)))


def clamp_clip_duration(value) -> int:
    return max(CLIP_DURATION_MIN_SECONDS, min(CLIP_DURATION_MAX_SECONDS, int(value)))


def _validated(key: str, value):
    """Returns a safe value for a persisted setting, or None if the stored value is unusable."""
    try:
        if key == "FPS_NOMINAL":
            return clamp_fps(value)
        if key == "CLIP_DURATION_SECONDS":
            return clamp_clip_duration(value)
        if key in ("OVERLAY_X", "OVERLAY_Y"):
            return max(0, min(100000, int(value))) if not isinstance(value, bool) else None
        if key == "VIDEO_BITRATE_KBPS":
            return clamp_bitrate(value) if not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None
    if key in ("REPLAY_ENABLED", "BURN_FPS_ON_RECORDING", "SHOW_FPS_ON_PREVIEW", "OVERLAY_ENABLED",
               "AUDIO_MIC_ENABLED"):
        return value if isinstance(value, bool) else None
    if key == "OUTPUT_RESOLUTION":
        return value if value in OUTPUT_RESOLUTIONS else None
    if key == "VIDEO_QUALITY":
        return value if value in VIDEO_QUALITIES else None
    if key == "AUDIO_DESKTOP_MODE":
        return value if value in AUDIO_DESKTOP_MODES else None
    if key == "AUDIO_TRACK_LAYOUT":
        return value if value in AUDIO_TRACK_LAYOUTS else None
    if key in ("AUDIO_INCLUDED_APPS", "AUDIO_BLOCKED_APPS") and isinstance(value, list):
        return sorted({v.strip().lower() for v in value if isinstance(v, str) and v.strip()})
    if key == "OVERLAY_STATS" and isinstance(value, dict):
        merged = dict(OVERLAY_STATS)
        merged.update({k: v for k, v in value.items() if k in merged and isinstance(v, bool)})
        return merged
    if key == "OVERLAY_POSITION":
        return value if value in OVERLAY_POSITIONS else None
    if key == "OVERLAY_MONITOR":
        return value if value in OVERLAY_MONITORS else None
    if key == "HOTKEYS" and isinstance(value, dict):
        # Combinations are checked against the keyboard library when they are registered.
        merged = dict(DEFAULT_HOTKEYS)
        merged.update({k: v.strip().lower() for k, v in value.items() if k in merged and isinstance(v, str)})
        return merged
    return None


def load_user_settings() -> None:
    """Applies saved UI settings over the defaults in this module."""
    try:
        data = json.loads(USER_SETTINGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        print(f"Warning: could not read {USER_SETTINGS_PATH.name} ({e}). Using defaults.")
        return
    if not isinstance(data, dict):
        return

    module_globals = globals()
    for key in PERSISTED_SETTINGS:
        if key in data:
            value = _validated(key, data[key])
            if value is not None:
                module_globals[key] = value
            else:
                print(f"Warning: ignoring invalid saved setting {key}.")


def save_user_settings() -> None:
    """Writes the current values of the persisted settings to user_settings.json."""
    module_globals = globals()
    data = {key: module_globals[key] for key in PERSISTED_SETTINGS}
    temp_path = USER_SETTINGS_PATH.with_suffix(".json.tmp")
    try:
        temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temp_path, USER_SETTINGS_PATH)
    except OSError as e:
        print(f"Warning: could not save settings: {e}")


def ensure_directories() -> None:
    """Create recording, clipping and temporary directories if they do not exist."""
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    os.makedirs(CLIPS_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
