"""
Configuration settings for the screen recorder application.
"""
import os
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent
RECORDINGS_DIR = BASE_DIR / "Recordings"
CLIPS_DIR = BASE_DIR / "Clips"

# Video capture settings (30 FPS standard, wall-clock synchronized)
FPS_NOMINAL = 30
CLIP_DURATION_SECONDS = 30
BUFFER_SIZE = FPS_NOMINAL * CLIP_DURATION_SECONDS
MONITOR_INDEX = 1

# Video output settings (mp4v is natively supported by OpenCV MP4 container)
FOURCC_CODEC = "mp4v"
VIDEO_EXTENSION = ".mp4"

# Overlays and Watermarks
BURN_FPS_ON_RECORDING = False
SHOW_FPS_ON_PREVIEW = True

# Controls / Global Hotkeys
HOTKEY_RECORD_TOGGLE = "f9"
HOTKEY_CLIP = "~"
HOTKEY_PAUSE = "p"

# Display settings (Simple Black & Gray Contrast Studio Window)
SHOW_PREVIEW = True
WINDOW_NAME = "Screen Recorder Studio"
WINDOW_WIDTH = 1360
WINDOW_HEIGHT = 768


def ensure_directories() -> None:
    """Create recording and clipping directories if they do not exist."""
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    os.makedirs(CLIPS_DIR, exist_ok=True)
