"""
Video writer utilities for creating and managing video output files.
Provides both direct and high-performance asynchronous multi-threaded VideoWriters,
plus timestamp-based assembly and muxing of audio tracks with the video.

Video is encoded in-process by PyAV with an FFmpeg H.264 encoder (NVENC, Quick Sync, AMF
or x264, whichever measures fast enough at the current settings), at the bitrate chosen by
the quality setting. Each distinct image is stored once with its real timestamp (variable
frame rate), so a busy encoder never builds up a backlog. Without PyAV, frames are piped
into the ffmpeg binary at a constant rate; OpenCV's mp4v writer is the last fallback (it
has no bitrate control).

Audio arrives as separate tracks (desktop, microphone, individual apps). Each track is
placed on the video timeline by its own timestamps into its own WAV, and the tracks are
mixed into one AAC stream (plus, optionally, one stream per source) when muxing.
"""
from collections import deque
from datetime import datetime
import os
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union
import wave
import cv2
import numpy as np
import time
import config

AudioFormat = Tuple[int, int]  # (sample_rate, channels)
AudioChunk = Tuple[float, str, np.ndarray]  # (capture_time, track id, int16 samples shaped (frames, channels))
TimedFrame = Tuple[float, np.ndarray]  # (capture_time, frame)
Encoder = Tuple[str, Dict]  # (display name, {"spec": EncoderSpec, "kbps": bitrate})
AudioTrackFile = Tuple[Path, str]  # (WAV path, track id)

# Pass as an encoder to pick the configured one when the writer opens (on its own thread).
AUTO_ENCODER = "auto"

# WAV sizes are 32-bit, so audio stops being written before the format's 4 GiB limit.
MAX_WAV_BYTES = 4_000_000_000
# Brief scheduling hiccups must not cause audible corrections: audio is only moved once
# it has been out of tolerance for this many chunks, or by more than this many seconds.
RESYNC_AFTER_CHUNKS = 5
RESYNC_IMMEDIATELY_SECONDS = 0.1
# How much video may wait for the encoder before frames are skipped (see AsyncVideoWriter).
WRITER_QUEUE_SECONDS = 0.5


def lower_current_thread_priority() -> None:
    """Runs the calling thread below normal priority (exports), so capture is scheduled first."""
    try:
        import win32_api
        win32_api.set_current_thread_priority(win32_api.THREAD_PRIORITY_BELOW_NORMAL)
    except Exception:
        pass


def generate_timestamped_filename(prefix: str, extension: str = ".mp4") -> str:
    """Generates a filename with the current date and time timestamp."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = extension if extension.startswith(".") else f".{extension}"
    return f"{prefix}_{timestamp}{ext}"


def create_video_writer(
    output_path: Union[str, Path],
    fourcc_str: str,
    fps: float,
    dimensions: Tuple[int, int],
) -> cv2.VideoWriter:
    """
    Initializes a synchronous cv2.VideoWriter instance.

    :param output_path: Destination path for the video file.
    :param fourcc_str: 4-character code string, e.g. 'mp4v' or 'XVID'.
    :param fps: Target nominal frames per second.
    :param dimensions: Tuple of (width, height).
    :return: An opened cv2.VideoWriter object.
    """
    fourcc = cv2.VideoWriter_fourcc(*fourcc_str)
    return cv2.VideoWriter(str(output_path), fourcc, fps, dimensions)


_ffmpeg_path: Optional[str] = None
_ffmpeg_looked_up = False

# Exports, muxing and probes run below normal priority so they never take CPU time from
# live capture and the live encoders.
BACKGROUND_PROCESS_FLAGS = subprocess.CREATE_NO_WINDOW | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)


def ffmpeg_executable() -> Optional[str]:
    """Returns the bundled ffmpeg binary's path (looked up once), or None if imageio-ffmpeg is missing."""
    global _ffmpeg_path, _ffmpeg_looked_up
    if not _ffmpeg_looked_up:
        _ffmpeg_looked_up = True
        try:
            import imageio_ffmpeg
            _ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as e:
            print(f"Warning: ffmpeg is unavailable ({e}). Install imageio-ffmpeg to mux audio.")
    return _ffmpeg_path


def run_ffmpeg(arguments: List[str]) -> Tuple[bool, str]:
    """Runs ffmpeg without a console window. Returns (succeeded, error output)."""
    ffmpeg_path = ffmpeg_executable()
    if ffmpeg_path is None:
        return False, "ffmpeg is unavailable"
    try:
        result = subprocess.run(
            [ffmpeg_path, "-y", "-loglevel", "error", *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=BACKGROUND_PROCESS_FLAGS,
        )
    except OSError as e:
        return False, str(e)
    return result.returncode == 0, result.stderr.strip()


# ----------------------------------------------------------------------------
# Audio tracks
# ----------------------------------------------------------------------------

def track_label(track: str) -> str:
    """Human-readable name of a track id ("desktop", "mic", "app:<exe>:<pid>")."""
    if track == "desktop":
        return "Desktop audio"
    if track == "mic":
        return "Microphone"
    if track.startswith("app:"):
        return track.split(":")[1]
    return track


def track_sort_key(track: str) -> Tuple[int, str]:
    order = 0 if track == "desktop" else 1 if track.startswith("app:") else 2 if track == "mic" else 3
    return order, track


def track_slug(track: str) -> str:
    """A filename-safe form of a track id."""
    return re.sub(r"[^A-Za-z0-9]+", "_", track).strip("_") or "track"


def audio_output_arguments() -> List[str]:
    """ffmpeg arguments for the AAC track(s) every output file gets."""
    return [
        "-c:a", "aac",
        "-b:a", config.AUDIO_BITRATE,
        "-ar", str(config.AUDIO_SAMPLE_RATE),
        "-ac", str(config.AUDIO_CHANNELS),
    ]


def audio_map_arguments(tracks: List[AudioTrackFile], first_input: int = 1) -> List[str]:
    """
    ffmpeg mapping for the video (input 0) and the audio tracks (inputs first_input...).
    One track is copied straight through. Several are summed into one "Mixed" stream
    (amix's averaging is undone with volume=N, so each source keeps its own level), and
    with AUDIO_TRACK_LAYOUT = "separate" each source is also kept as its own stream.
    """
    if len(tracks) == 1:
        return ["-map", "0:v:0", "-map", f"{first_input}:a:0",
                "-metadata:s:a:0", f"title={track_label(tracks[0][1])}"]
    count = len(tracks)
    inputs = "".join(f"[{first_input + i}:a]" for i in range(count))
    graph = f"{inputs}amix=inputs={count}:duration=longest:dropout_transition=0,volume={count}[mix]"
    arguments = ["-filter_complex", graph, "-map", "0:v:0", "-map", "[mix]", "-metadata:s:a:0", "title=Mixed"]
    if config.AUDIO_TRACK_LAYOUT == "separate":
        for i, (_, track) in enumerate(tracks):
            arguments += ["-map", f"{first_input + i}:a:0", f"-metadata:s:a:{i + 1}", f"title={track_label(track)}"]
    return arguments


def mux_audio_video(video_path: Optional[Path], audio_tracks: List[AudioTrackFile], output_path: Path,
                    video_input: Optional[List[str]] = None, faststart: bool = True) -> bool:
    """
    Combines a video file with one or more WAV tracks, copying the video stream and
    encoding AAC audio. The output only appears at output_path once complete.

    :param video_input: ffmpeg input arguments to use instead of "-i video_path"
                        (e.g. a concat list), so joining and muxing happen in one pass.
    :param faststart: Moves the index to the front for streaming. This rewrites the
                      whole file once more, so long local clips skip it.
    :return: True if the output file was created.
    """
    partial_path = Path(config.TEMP_DIR) / f"{output_path.stem}.muxed{output_path.suffix}"
    arguments = [*(video_input if video_input is not None else ["-i", str(video_path)])]
    for path, _ in audio_tracks:
        arguments += ["-i", str(path)]
    arguments += [
        *audio_map_arguments(audio_tracks),
        "-c:v", "copy",
        *audio_output_arguments(),
        *(["-movflags", "+faststart"] if faststart else []),
        str(partial_path),
    ]

    succeeded, error = run_ffmpeg(arguments)
    if not succeeded:
        print(f"Warning: ffmpeg could not mux {output_path.name}: {error}")
        partial_path.unlink(missing_ok=True)
        return False
    try:
        os.replace(partial_path, output_path)
        return True
    except OSError as e:
        print(f"Warning: could not mux {output_path.name}: {e}")
        partial_path.unlink(missing_ok=True)
        return False


# ----------------------------------------------------------------------------
# Video encoders: bitrate, candidates and the speed-based choice
# ----------------------------------------------------------------------------

def target_bitrate_kbps(dimensions: Tuple[int, int], fps: float, quality: Optional[str] = None,
                        custom_kbps: Optional[int] = None) -> int:
    """
    Video bitrate for a quality setting (the configured one by default). Presets are
    defined for 1080p at 30 FPS and scale with the pixel count and (less than linearly)
    with frame rate, since consecutive frames at high rates differ less.
    """
    quality = quality or config.VIDEO_QUALITY
    if quality == "custom":
        return int(custom_kbps if custom_kbps is not None else config.VIDEO_BITRATE_KBPS)
    mbps = config.VIDEO_QUALITY_PRESETS.get(quality, config.VIDEO_QUALITY_PRESETS["high"])
    width, height = dimensions
    pixels = (width * height) / (1920 * 1080)
    fps_factor = (max(1.0, float(fps)) / 30.0) ** 0.6
    return max(500, int(round(mbps * 1000 * pixels * fps_factor)))


def _rate_options(kbps: int) -> Dict[str, str]:
    """Variable bitrate around the target, with headroom for busy scenes. No B-frames: packets
    then come out in display order, which keeps per-frame timestamps and durations simple."""
    return {"b": f"{kbps}k", "maxrate": f"{kbps * 3 // 2}k", "bufsize": f"{kbps * 2}k", "bf": "0"}


class EncoderSpec:
    """One way to encode H.264: an FFmpeg encoder, its input pixel format and its options."""

    def __init__(self, name: str, codec: str, pix_fmt: str, options: Dict[str, str], hardware: bool):
        self.name = name
        self.codec = codec
        self.pix_fmt = pix_fmt
        self.options = options
        self.hardware = hardware

    def options_for(self, kbps: int) -> Dict[str, str]:
        return {**self.options, **_rate_options(kbps)}

    def cli_arguments(self, kbps: int) -> List[str]:
        """The same settings as ffmpeg command-line arguments (for the pipe fallback)."""
        arguments = ["-c:v", self.codec]
        for key, value in self.options_for(kbps).items():
            arguments += [f"-{key}:v" if key == "b" else f"-{key}", value]
        return arguments + ["-pix_fmt", self.pix_fmt]


# In order of preference. Which one is used is decided by measuring each at the actual
# resolution and frame rate (see video_encoder): on some PCs a hardware encoder is slower
# than x264 (e.g. Quick Sync on a laptop's integrated GPU while a game uses it).
ENCODER_SPECS: List[EncoderSpec] = [
    EncoderSpec("NVIDIA NVENC", "h264_nvenc", "yuv420p", {"preset": "p3", "rc": "vbr"}, True),
    EncoderSpec("Intel Quick Sync", "h264_qsv", "nv12",
                {"preset": "veryfast", "low_power": "1", "look_ahead": "0", "async_depth": "4"}, True),
    EncoderSpec("Intel Quick Sync (standard)", "h264_qsv", "nv12", {"preset": "veryfast"}, True),
    EncoderSpec("AMD AMF", "h264_amf", "yuv420p", {"quality": "speed", "rc": "vbr_peak"}, True),
    EncoderSpec("x264 (superfast)", "libx264", "yuv420p", {"preset": "superfast"}, False),
    EncoderSpec("x264 (ultrafast)", "libx264", "yuv420p", {"preset": "ultrafast"}, False),
]
_SPECS_BY_NAME = {spec.name: spec for spec in ENCODER_SPECS}
# Kept for tools that list the encoders: (name, builder of ffmpeg arguments for a bitrate).
ENCODER_CANDIDATES: List[Tuple[str, Callable[[int], List[str]]]] = [
    (spec.name, spec.cli_arguments) for spec in ENCODER_SPECS
]

try:
    import av  # PyAV: in-process encoding with exact per-frame timestamps
    from fractions import Fraction
except ImportError:
    av = None
    print("Note: PyAV isn't installed (pip install av); using the slower ffmpeg pipe for video.")

_selection_lock = threading.Lock()
_last_choice: Optional[str] = None      # For display (Settings > System info)
_choices: Dict[Tuple, Optional[str]] = {}  # (size, fps, kbps) -> chosen encoder name, this session
# Encoders that failed to start on real input this session; they're skipped from then on.
_failed_encoders: Set[str] = set()

# The speed test encodes moving, detailed frames through the real writer at the real size,
# frame rate and bitrate. Some hardware encoders accept small test frames but fail or crawl
# on real input, which a lighter test would miss.
PROBE_FRAMES = 36
PROBE_WARMUP_FRAMES = 6
# An encoder must manage this multiple of the frames per second it will get, leaving room
# for scene changes and for a second file (the replay buffer) encoding at the same time.
SPEED_HEADROOM = 1.5
# Measured speeds are kept in Temp/ for a day, so startup doesn't re-test every time. A
# hardware encoder's test can also briefly disturb screen capture, another reason to cache it.
SPEED_CACHE_SECONDS = 24 * 3600
SPEED_CACHE_VERSION = 2


def _candidates() -> List[EncoderSpec]:
    if config.CLIP_ENCODER == "x264":
        return [spec for spec in ENCODER_SPECS if spec.codec == "libx264"]
    return ENCODER_SPECS


def _backend() -> str:
    return "pyav" if av is not None else "pipe"


def _speed_cache_path() -> Path:
    return Path(config.TEMP_DIR) / "encoder_speeds.json"


def _load_speed_cache() -> Dict[str, Dict]:
    import json
    try:
        data = json.loads(_speed_cache_path().read_text(encoding="utf-8"))
        if data.get("version") == SPEED_CACHE_VERSION:
            return data.get("results", {})
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def _save_speed_cache(results: Dict[str, Dict]) -> None:
    import json
    try:
        path = _speed_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": SPEED_CACHE_VERSION, "results": results}, indent=1), encoding="utf-8")
    except OSError:
        pass


_probe_frames_cache: Dict[Tuple[int, int], List[np.ndarray]] = {}


def _probe_frames(size: Tuple[int, int]) -> List[np.ndarray]:
    """Detailed, moving frames: a smooth random texture scrolling sideways."""
    frames = _probe_frames_cache.get(size)
    if frames is None:
        width, height = size
        texture = np.random.default_rng(7).integers(0, 255, (max(2, height // 8), max(2, width // 8), 3),
                                                    dtype=np.uint8)
        base = cv2.resize(texture, (width, height), interpolation=cv2.INTER_CUBIC)
        frames = [np.roll(base, i * 9, axis=1) for i in range(12)]
        _probe_frames_cache.clear()
        _probe_frames_cache[size] = frames
    return frames


def _measure_encoder(spec: EncoderSpec, size: Tuple[int, int], fps: float, kbps: int) -> float:
    """Frames per second this encoder sustains at these settings (0 if it doesn't work).
    Runs silently: a failure only means the encoder isn't used."""
    temp_dir = Path(config.TEMP_DIR)
    temp_dir.mkdir(parents=True, exist_ok=True)
    path = temp_dir / f"encoder_probe_{os.getpid()}_{threading.get_ident()}.mp4"
    frames = _probe_frames(size)
    writer = _open_encoder_writer(path, fps, size, spec, kbps, background=True, report_errors=False)
    started = None
    try:
        for i in range(PROBE_FRAMES):
            if i == PROBE_WARMUP_FRAMES:
                started = time.perf_counter()  # Encoder start-up isn't part of its speed
            writer.write(frames[i % len(frames)], i)
            if writer.failed:
                break
        # Timed until the encoder has finished every frame: encoders buffer input, so the
        # writes alone would make a slow encoder look fast.
        writer.release(PROBE_FRAMES)
        elapsed = time.perf_counter() - started if started is not None else 0.0
        works = not writer.failed and writer.produced_output()
    except Exception:
        works, elapsed = False, 0.0
        try:
            writer.abort()
        except Exception:
            pass
    path.unlink(missing_ok=True)
    if not works or elapsed <= 0:
        return 0.0
    return (PROBE_FRAMES - PROBE_WARMUP_FRAMES) / elapsed


def _expected_frame_rate(fps: float) -> float:
    """Frames per second an encoder will actually get: no more than the display shows."""
    try:
        import win32_api
        refresh = win32_api.display_refresh_rate()
    except Exception:
        refresh = 0
    return min(float(fps), float(refresh)) if refresh else float(fps)


def report_encoder_failure(name: Optional[str]) -> None:
    """Marks an encoder that couldn't start on real input, so the next file uses the next candidate."""
    if not name:
        return
    with _selection_lock:
        if name in _failed_encoders:
            return
        _failed_encoders.add(name)
        for key in [k for k, chosen in _choices.items() if chosen == name]:
            del _choices[key]
    print(f"Warning: the {name} encoder failed to start; switching to the next available encoder.")


def clip_encoder_name() -> str:
    """The encoder chosen most recently, for display. Never waits for a running speed test
    (the dashboard calls this), so it reads without the lock."""
    if _last_choice is None and not _choices:
        return "detecting..."
    return _last_choice if _last_choice is not None else "OpenCV mp4v (no bitrate control)"


def clip_encoder() -> Optional[str]:
    """Name of the encoder used at 1080p 60 FPS, the common case (testing it if needed)."""
    choice = video_encoder((1920, 1080), 60)
    return choice[0] if choice is not None else None


def prepare_clip_encoder(dimensions: Optional[Tuple[int, int]] = None, fps: Optional[float] = None) -> None:
    """Chooses the encoder for these settings on a background thread, so the first recording
    or replay segment doesn't wait for the speed test."""
    if dimensions is None or fps is None:
        target = clip_encoder
    else:
        def target():
            video_encoder(dimensions, fps)
    threading.Thread(target=target, daemon=True, name="EncoderSelection").start()


def video_encoder(dimensions: Tuple[int, int], fps: float) -> Optional[Encoder]:
    """
    (name, settings) of the encoder to use at these settings and the current quality, or None
    for OpenCV. Each candidate's speed is measured here (once per settings, cached for a day):
    the first in preference order that keeps up with comfortable headroom is used; if none
    does, the fastest is. May block for a few seconds on first use: call it off the main thread.
    """
    global _last_choice
    if config.CLIP_ENCODER == "opencv":
        return None
    size = (int(dimensions[0]), int(dimensions[1]))
    kbps = target_bitrate_kbps(size, fps)
    key = (size, float(fps), kbps)
    with _selection_lock:
        if key in _choices:
            name = _choices[key]
            _last_choice = name
            return (name, {"spec": _SPECS_BY_NAME[name], "kbps": kbps}) if name else None

        needed = _expected_frame_rate(fps) * SPEED_HEADROOM
        cache = _load_speed_cache()
        now = time.time()
        chosen: Optional[EncoderSpec] = None
        fastest: Optional[Tuple[float, EncoderSpec]] = None
        measured = []
        for spec in _candidates():
            if spec.name in _failed_encoders:
                continue
            cache_key = f"{_backend()}|{spec.name}|{size[0]}x{size[1]}|{fps:g}|{kbps}"
            entry = cache.get(cache_key)
            if entry is not None and now - entry.get("time", 0) < SPEED_CACHE_SECONDS:
                speed = float(entry.get("fps", 0))
            else:
                speed = _measure_encoder(spec, size, fps, kbps)
                cache[cache_key] = {"fps": round(speed, 1), "time": now}
            if speed <= 0:
                continue
            measured.append(f"{spec.name} {speed:.0f}")
            if fastest is None or speed > fastest[0]:
                fastest = (speed, spec)
            if speed >= needed:
                chosen = spec
                break
        _save_speed_cache(cache)
        if chosen is None and fastest is not None:
            chosen = fastest[1]
            print(f"Note: no encoder keeps up with {fps:g} FPS at {size[0]}x{size[1]} with headroom "
                  f"(measured FPS: {', '.join(measured)}). Using the fastest; the recording will show "
                  f"fewer distinct frames, evenly spaced and correctly timed.")
        name = chosen.name if chosen is not None else None
        _choices[key] = name
        _last_choice = name
        if chosen is None:
            print("Video encoder: OpenCV (no FFmpeg H.264 encoder works)")
            return None
        print(f"Video encoder for {size[0]}x{size[1]} at {fps:g} FPS: {name} ({'; '.join(measured)} FPS measured)")
        return name, {"spec": chosen, "kbps": kbps}


class FFmpegPipeWriter:
    """
    cv2.VideoWriter-compatible writer that pipes raw frames into an ffmpeg encoder.
    Encoding runs in the ffmpeg process (multithreaded or on the GPU), not in Python.

    Frames with even dimensions are converted to I420 (yuv420p) here, with OpenCV's SIMD,
    multithreaded cvtColor (which releases the GIL). That halves the bytes pushed through
    the pipe and spares ffmpeg its single-threaded colour conversion, which is what limits
    high resolutions and frame rates. The conversion matches ffmpeg's default (BT.601,
    limited range). A frame written several times in a row is converted only once.
    """

    def __init__(self, output_path: Union[str, Path], fps: float, dimensions: Tuple[int, int],
                 encoder_arguments: List[str], background: bool = False, encoder_name: Optional[str] = None,
                 report_errors: bool = True):
        """
        background: run below normal priority (exports), so live capture keeps the CPU.
        encoder_name: if this encoder produces nothing at all, it's reported as failed so
                      later files use the next one.
        """
        width, height = dimensions
        self.failed = False
        self.output_path = Path(output_path)
        self.encoder_name = encoder_name
        self._report_errors = report_errors
        self._yuv = width % 2 == 0 and height % 2 == 0
        self._last_source: Optional[np.ndarray] = None
        self._last_data = None
        if self._yuv:
            input_format, crop = "yuv420p", []
        else:
            input_format, crop = "bgr24", ["-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2"]
        command = [
            ffmpeg_executable() or "ffmpeg", "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", input_format, "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
            *crop, *encoder_arguments, "-an", str(output_path),
        ]
        self._errors = tempfile.TemporaryFile()
        try:
            self._process: Optional[subprocess.Popen] = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self._errors,
                bufsize=0,  # Frames are written whole; Python-side buffering would only add a copy
                creationflags=BACKGROUND_PROCESS_FLAGS if background else subprocess.CREATE_NO_WINDOW,
            )
        except OSError as e:
            print(f"Warning: could not start the ffmpeg encoder: {e}")
            self._process = None
            self.failed = True

    def isOpened(self) -> bool:
        return not self.failed

    def _frame_data(self, frame: np.ndarray):
        if frame is self._last_source:
            return self._last_data  # Held frame repeated to fill missed slots
        if self._yuv:
            data = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420).data
        else:
            data = np.ascontiguousarray(frame).data
        self._last_source, self._last_data = frame, data
        return data

    def write(self, frame: np.ndarray) -> None:
        if self.failed:
            return
        try:
            data = memoryview(self._frame_data(frame)).cast("B")
            stdin = self._process.stdin
            while data:
                # Unbuffered writes may be partial, so write until the whole frame is in.
                written = stdin.write(data)
                if not written:
                    raise BrokenPipeError("ffmpeg stopped reading")
                data = data[written:]
        except (OSError, ValueError):  # Includes BrokenPipeError when ffmpeg exits early
            self.failed = True

    def abort(self) -> None:
        """Stops the encoder without finishing the file (a writer that was never used)."""
        self._last_source = self._last_data = None
        self.failed = True
        if self._process is not None:
            try:
                self._process.kill()
                self._process.wait(timeout=5.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                self._process.stdin.close()
            except OSError:
                pass
            self._process = None
        self._errors.close()

    def release(self) -> None:
        self._last_source = self._last_data = None
        if self._process is None:
            self._errors.close()
            return
        try:
            self._process.stdin.close()
        except OSError:
            self.failed = True
        if self._process.wait() != 0:
            self.failed = True
            if self._report_errors:
                self._errors.seek(0)
                message = self._errors.read().decode("utf-8", "replace").strip()
                print(f"Warning: ffmpeg encoder failed: {message[-500:]}")
        self._errors.close()
        self._process = None
        if self.failed and not self.produced_output():
            report_encoder_failure(self.encoder_name)

    def produced_output(self) -> bool:
        """True if the output file exists and has data (the encoder at least started)."""
        try:
            return self.output_path.stat().st_size > 0
        except OSError:
            return False


# ----------------------------------------------------------------------------
# Slot writers
# ----------------------------------------------------------------------------
#
# Every writer the app uses takes frames with a *slot*: the frame's index on the output's
# constant-rate timeline (slot / fps = its time in the file). write(frame, slot) means "this
# image is on screen from this slot until the next write", and release(end_slot) ends the
# file at end_slot. Slots must increase.
#
# PyAVWriter stores each image once with its slot's timestamp (variable frame rate), so an
# image held for several slots costs nothing to encode. That matters: a constant-rate file
# must encode fps frames for every second of real time, so an encoder slightly slower than
# that falls further and further behind, and the backlog ends in long freezes. Here an
# overloaded encoder just gets fewer, evenly spaced, correctly timed frames.
#
# SlotWriter adapts the constant-rate writers (the ffmpeg pipe, OpenCV) by writing the held
# image into every slot.


class PyAVWriter:
    """Encodes in-process with PyAV (FFmpeg's libraries), one packet per distinct image."""

    TIME_BASE_DENOMINATOR = 90000  # Exact for 15..240 FPS rates that divide it; others round to 11 us
    POOL_SIZE = 16  # Reused frames; hardware encoders may still hold a few submitted ones

    def __init__(self, output_path: Union[str, Path], fps: float, dimensions: Tuple[int, int], spec: EncoderSpec,
                 kbps: int, encoder_name: Optional[str] = None, report_errors: bool = True):
        width, height = dimensions
        # 4:2:0 video needs even dimensions; an odd last row/column is dropped.
        self._width, self._height = width - width % 2, height - height % 2
        self.output_path = Path(output_path)
        self.encoder_name = encoder_name
        self.failed = False
        self._report_errors = report_errors
        self._fps = float(fps)
        self._nv12 = spec.pix_fmt == "nv12"
        self._time_base = Fraction(1, self.TIME_BASE_DENOMINATOR)
        self._pending = None  # Last packet, muxed once the next one tells its duration
        self._last_slot = -1
        self._pool: List = []
        self._pool_index = 0
        self._container = None
        self._stream = None
        h, w = self._height, self._width
        self._i420 = np.empty((h * 3 // 2, w), dtype=np.uint8)
        self._uv = np.empty((h // 2, w // 2, 2), dtype=np.uint8) if self._nv12 else None
        try:
            self._container = av.open(str(self.output_path), "w", format="mp4")
            rate = Fraction(self._fps).limit_denominator(1001)
            stream = self._container.add_stream(spec.codec, rate=rate, options=spec.options_for(kbps))
            stream.width, stream.height, stream.pix_fmt = w, h, spec.pix_fmt
            stream.codec_context.time_base = self._time_base
            stream.time_base = self._time_base
            stream.codec_context.gop_size = max(1, int(round(self._fps * 2)))  # A keyframe every 2 s for seeking
            stream.bit_rate = int(kbps) * 1000
            self._stream = stream
            self._pool = [av.VideoFrame(w, h, spec.pix_fmt) for _ in range(self.POOL_SIZE)]
        except Exception as e:
            self._fail(f"could not start {spec.name}: {e}")

    # --- Interface --------------------------------------------------------------------

    def isOpened(self) -> bool:
        return not self.failed

    def write(self, frame: np.ndarray, slot: int) -> None:
        if self.failed or slot <= self._last_slot:
            return
        self._last_slot = slot
        try:
            av_frame = self._next_pool_frame()
            self._fill(av_frame, frame)
            av_frame.pts = self._pts(slot)
            av_frame.time_base = self._time_base
            self._mux(self._stream.encode(av_frame))
        except Exception as e:
            self._fail(f"encoding failed: {e}")

    def release(self, end_slot: Optional[int] = None) -> None:
        """Finishes the file; it lasts until end_slot (default: one slot after the last image)."""
        if self._container is None:
            self._after_close()
            return
        try:
            if not self.failed:
                self._mux(self._stream.encode())  # Flush the encoder
                if self._pending is not None:
                    end = self._pts(end_slot if end_slot is not None else self._last_slot + 1)
                    self._pending.duration = max(1, end - self._pending.pts)
                    self._container.mux(self._pending)
                    self._pending = None
        except Exception as e:
            self._fail(f"finishing failed: {e}")
        try:
            self._container.close()
        except Exception as e:
            if not self.failed:
                self._fail(f"closing failed: {e}")
        self._after_close()

    def abort(self) -> None:
        """Stops without finishing the file (a writer that was never used)."""
        self.failed = True
        if self._container is not None:
            try:
                self._container.close()
            except Exception:
                pass
        self._container = None
        self._pool = []
        self._pending = None

    def produced_output(self) -> bool:
        try:
            return self.output_path.stat().st_size > 0
        except OSError:
            return False

    # --- Internals --------------------------------------------------------------------

    def _pts(self, slot: int) -> int:
        return int(round(slot * self.TIME_BASE_DENOMINATOR / self._fps))

    def _next_pool_frame(self):
        av_frame = self._pool[self._pool_index]
        self._pool_index = (self._pool_index + 1) % len(self._pool)
        make_writable = getattr(av_frame, "make_writable", None)
        if make_writable is not None:
            make_writable()  # If the encoder still holds this buffer, it gets a fresh one
        return av_frame

    def _fill(self, av_frame, frame: np.ndarray) -> None:
        """BGR -> the encoder's 4:2:0 format, written into the frame's own planes."""
        h, w = self._height, self._width
        if frame.shape[0] != h or frame.shape[1] != w:
            frame = frame[:h, :w]
        cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420, dst=self._i420)
        y = self._i420[:h]
        u = self._i420[h:h + h // 4].reshape(h // 2, w // 2)
        v = self._i420[h + h // 4:].reshape(h // 2, w // 2)
        if self._nv12:
            cv2.merge((u, v), dst=self._uv)  # Interleaved UV plane
            planes = (y, self._uv.reshape(h // 2, w))
        else:
            planes = (y, u, v)
        for plane, data in zip(av_frame.planes, planes):
            _copy_plane(plane, data)

    def _mux(self, packets) -> None:
        for packet in packets:
            pending = self._pending
            if pending is not None:
                # Without B-frames packets arrive in display order, so each one's duration
                # runs until the next. The last one's is set when the file ends.
                pending.duration = max(1, packet.pts - pending.pts)
                self._container.mux(pending)
            self._pending = packet

    def _fail(self, message: str) -> None:
        if not self.failed and self._report_errors:
            print(f"Warning: video encoder ({self.encoder_name or 'test'}) {message}")
        self.failed = True

    def _after_close(self) -> None:
        self._container = None
        self._pool = []
        self._pending = None
        if self.failed and not self.produced_output():
            report_encoder_failure(self.encoder_name)


def _copy_plane(plane, data: np.ndarray) -> None:
    """Copies a 2-D uint8 array into a PyAV frame plane, which may have padded rows."""
    rows, cols = data.shape
    line = plane.line_size
    try:
        view = np.frombuffer(plane, dtype=np.uint8)[:rows * line].reshape(rows, line)
        view[:, :cols] = data
    except ValueError:  # Read-only buffer in this PyAV version
        if line == cols:
            plane.update(np.ascontiguousarray(data))
        else:
            padded = np.zeros((rows, line), dtype=np.uint8)
            padded[:, :cols] = data
            plane.update(padded)


class SlotWriter:
    """Gives a constant-rate writer (ffmpeg pipe or OpenCV) the slot interface by writing the
    held image into every slot up to the next one."""

    def __init__(self, inner, output_path: Union[str, Path]):
        self._inner = inner
        self.output_path = Path(output_path)
        self.encoder_name = getattr(inner, "encoder_name", None)
        self._next_slot = 0
        self._last: Optional[np.ndarray] = None

    @property
    def failed(self) -> bool:
        return bool(getattr(self._inner, "failed", False))

    def isOpened(self) -> bool:
        return self._inner.isOpened()

    def write(self, frame: np.ndarray, slot: int) -> None:
        if slot < self._next_slot:
            return
        held = self._last if self._last is not None else frame
        while self._next_slot < slot:
            self._inner.write(held)
            self._next_slot += 1
        self._inner.write(frame)
        self._next_slot = slot + 1
        self._last = frame

    def release(self, end_slot: Optional[int] = None) -> None:
        if end_slot is not None and self._last is not None:
            while self._next_slot < end_slot:
                self._inner.write(self._last)
                self._next_slot += 1
        self._last = None
        self._inner.release()

    def abort(self) -> None:
        self._last = None
        abort = getattr(self._inner, "abort", None)
        if abort is not None:
            abort()
        else:
            self._inner.release()

    def produced_output(self) -> bool:
        produced = getattr(self._inner, "produced_output", None)
        if produced is not None:
            return produced()
        try:
            return self.output_path.stat().st_size > 0
        except OSError:
            return False


def _open_encoder_writer(path: Union[str, Path], fps: float, dimensions: Tuple[int, int], spec: EncoderSpec,
                         kbps: int, background: bool = False, report_errors: bool = True,
                         encoder_name: Optional[str] = None):
    """A slot writer for one FFmpeg encoder: in-process with PyAV, or the ffmpeg pipe without it."""
    if av is not None:
        return PyAVWriter(path, fps, dimensions, spec, kbps, encoder_name=encoder_name, report_errors=report_errors)
    pipe = FFmpegPipeWriter(path, fps, dimensions, spec.cli_arguments(kbps), background=background,
                            encoder_name=encoder_name, report_errors=report_errors)
    return SlotWriter(pipe, path)


def open_video_writer(path: Union[str, Path], fourcc_str: str, fps: float, dimensions: Tuple[int, int],
                      encoder: Optional[Encoder], background: bool = False):
    """
    A slot writer (see above) for an encoder, or for OpenCV if there is none or it can't start.
    background: an export; the ffmpeg pipe then runs below normal priority.
    """
    if encoder is not None:
        name, settings = encoder
        writer = _open_encoder_writer(path, fps, dimensions, settings["spec"], settings["kbps"],
                                      background=background, encoder_name=name)
        if writer.isOpened():
            return writer
        writer.abort()
        Path(path).unlink(missing_ok=True)
        report_encoder_failure(name)
        print(f"Warning: {name} could not start. Using OpenCV for {Path(path).name}.")
    return SlotWriter(create_video_writer(path, fourcc_str, fps, dimensions), path)


def open_wav(path: Path, audio_format: AudioFormat) -> wave.Wave_write:
    """Opens a 16-bit PCM WAV file for writing."""
    sample_rate, channels = audio_format
    wav = wave.open(str(path), "wb")
    wav.setnchannels(channels)
    wav.setsampwidth(2)
    wav.setframerate(sample_rate)
    return wav


class FrameTimeline:
    """
    Maps capture timestamps onto a constant-frame-rate output timeline.

    Frame index follows real time: each frame is written as many times as needed to
    reach the slot its timestamp belongs to (or skipped if it's early). Gaps between
    frames longer than AV_GAP_CUT_SECONDS are cut, leaving one frame slot.
    """

    def __init__(self, fps: float):
        self.fps = fps
        self.first_ts: Optional[float] = None
        self.last_ts: Optional[float] = None
        self.cut_seconds = 0.0
        self.frames_written = 0

    def media_time(self, capture_time: float) -> float:
        """Position on the output timeline, in seconds, of a capture timestamp."""
        return capture_time - self.first_ts - self.cut_seconds

    def gap_start(self, previous_offered: Optional[float]) -> Optional[float]:
        """When capture last produced a frame before this one. Frames dropped because a queue
        was full still count: capture was running, so that time is real and must be kept."""
        if self.last_ts is None:
            return None
        if previous_offered is None or previous_offered < self.last_ts:
            return self.last_ts
        return previous_offered

    def is_gap(self, capture_time: float, previous_offered: Optional[float] = None) -> bool:
        """
        True if a frame at this time follows a gap that gets cut (a pause or a real stall).
        previous_offered: capture time of the frame offered just before this one, including a
        dropped one. Without it, an encoder backlog (many drops in a row) would look like a
        pause and be cut, making the file shorter than real time (sped up).
        """
        start = self.gap_start(previous_offered)
        return start is not None and capture_time - start > config.AV_GAP_CUT_SECONDS

    def advance(self, capture_time: float, previous_offered: Optional[float] = None) -> int:
        """Places a frame and returns how many times to write it (0 = skip)."""
        if self.first_ts is None:
            self.first_ts = capture_time
        elif self.is_gap(capture_time, previous_offered):
            self.cut_seconds += (capture_time - self.gap_start(previous_offered)) - 1.0 / self.fps
        self.last_ts = capture_time

        target_frames = round(self.media_time(capture_time) * self.fps) + 1
        repeats = target_frames - self.frames_written
        if repeats <= 0:
            return 0
        self.frames_written += repeats
        return repeats


class AudioTrackWriter:
    """Writes timestamped audio chunks to a WAV file, keeping them at their correct position."""

    # None disables the limit, for writers that split output across several files.
    max_bytes: Optional[int] = MAX_WAV_BYTES

    def __init__(self, path: Optional[Path], audio_format: AudioFormat, origin: int = 0):
        """origin: sample position the track starts at (a track that joins a running timeline)."""
        self.path = path
        self.sample_rate, self.channels = audio_format
        self.frames_written = origin
        self._bytes_per_frame = self.channels * 2
        self._tolerance = round(config.AUDIO_RESYNC_TOLERANCE_S * self.sample_rate)
        self._immediate = round(RESYNC_IMMEDIATELY_SECONDS * self.sample_rate)
        self._chunks_out_of_tolerance = 0
        self._placed_first_chunk = False
        self._full = False
        self._wav = open_wav(path, audio_format) if path is not None else None

    def add_chunk(self, media_start: float, samples: np.ndarray) -> None:
        """Writes a chunk that belongs media_start seconds into the output."""
        if self._full:
            return

        drift = round(media_start * self.sample_rate) - self.frames_written
        if not self._placed_first_chunk:
            # The track's start is always placed exactly, setting the alignment for what follows.
            self._placed_first_chunk = True
            correct = True
        elif abs(drift) <= self._tolerance:
            self._chunks_out_of_tolerance = 0
            correct = False
        else:
            self._chunks_out_of_tolerance += 1
            correct = abs(drift) > self._immediate or self._chunks_out_of_tolerance >= RESYNC_AFTER_CHUNKS

        if correct:
            self._chunks_out_of_tolerance = 0
            if drift > 0:
                self._write_silence(drift)
            elif drift < 0:
                samples = samples[-drift:]

        if len(samples):
            self._write_bytes(samples.tobytes())

    def pad_to_duration(self, duration: float) -> None:
        """Adds trailing silence so the track is at least this many seconds long."""
        self.pad_to_samples(round(duration * self.sample_rate))

    def pad_to_samples(self, total_frames: int) -> None:
        """Adds trailing silence so the track holds at least this many sample frames."""
        missing = total_frames - self.frames_written
        if missing > 0:
            self._write_silence(missing)

    def close(self) -> None:
        if self._wav is not None:
            self._wav.close()

    def _write_silence(self, frames: int) -> None:
        # Written in one-second blocks so a long silent stretch needs little memory.
        one_second = bytes(self.sample_rate * self._bytes_per_frame)
        while frames > 0 and not self._full:
            count = min(frames, self.sample_rate)
            self._write_bytes(one_second[:count * self._bytes_per_frame])
            frames -= count

    def _write_bytes(self, data: bytes) -> None:
        if self.max_bytes is not None and (self.frames_written * self._bytes_per_frame) + len(data) > self.max_bytes:
            print(f"Warning: audio size limit reached. The rest of {self.path.stem} has no audio.")
            self._full = True
            return
        self._emit(data)
        self.frames_written += len(data) // self._bytes_per_frame

    def _emit(self, data: bytes) -> None:
        """Stores placed audio. Subclasses can route it elsewhere."""
        self._wav.writeframesraw(data)


class AVAssembler:
    """
    Builds one output file from timestamped frames and audio chunks.

    Frames are repeated or skipped so that frame index follows real time at the constant
    output FPS, and each audio track is placed on the same timeline. Gaps between frames
    longer than AV_GAP_CUT_SECONDS (pause, stall) are removed from both video and audio.
    """

    def __init__(
        self,
        output_path: Union[str, Path],
        fourcc_str: str,
        fps: float,
        dimensions: Tuple[int, int],
        audio_format: Optional[AudioFormat],
        encoder: Union[None, str, Encoder] = None,
        keep_partial: bool = False,
        background: bool = False,
    ):
        """
        :param encoder: (name, ffmpeg arguments), None for OpenCV with fourcc_str, or
                        AUTO_ENCODER to use the configured encoder and quality.
        :param keep_partial: Keep what was encoded if the encoder fails partway (recordings),
                             instead of discarding it so the caller can retry (clips).
        :param background: Encode below normal priority (clip exports), so live capture
                           and recording keep the CPU.
        """
        self.background = background
        self.output_path = Path(output_path)
        self.fourcc_str = fourcc_str
        self.fps = fps
        self.dimensions = dimensions
        self.audio_format = audio_format
        self.encoder = encoder
        self.keep_partial = keep_partial
        self.encode_failed = False

        temp_dir = Path(config.TEMP_DIR)
        self._temp_dir = temp_dir
        self._temp_video = temp_dir / f"{self.output_path.stem}.video{self.output_path.suffix}"

        self._writer = None  # A slot writer (PyAVWriter or SlotWriter)
        self._audio_tracks: Dict[str, AudioTrackWriter] = {}
        self._failed_tracks: Set[str] = set()
        self._pending_audio: deque = deque()
        self._timeline = FrameTimeline(fps)
        self._previous_frame: Optional[np.ndarray] = None

    @property
    def frames_written(self) -> int:
        return self._timeline.frames_written

    def open(self) -> None:
        """Opens the output file. Called on the encoding thread, as it can take a moment."""
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        if self.encoder == AUTO_ENCODER:
            self.encoder = video_encoder(self.dimensions, self.fps)
        self._writer = open_video_writer(self._temp_video, self.fourcc_str, self.fps, self.dimensions, self.encoder,
                                         background=self.background)

    def add_audio(self, track: str, capture_time: float, samples: np.ndarray) -> None:
        """Queues an audio chunk. Safe to call from the audio threads."""
        self._pending_audio.append((capture_time, track, samples))

    def add_frame(self, capture_time: float, frame: np.ndarray, previous_offered: Optional[float] = None) -> None:
        """Places a frame on the output timeline, along with the audio captured up to it.
        previous_offered: see FrameTimeline.is_gap (writers that drop frames pass it)."""
        timeline = self._timeline
        if timeline.is_gap(capture_time, previous_offered):
            # Audio up to the gap uses the mapping from before the cut; audio inside it is dropped.
            # Audio from before the gap (including while frames were being dropped) is kept.
            self._flush_audio(timeline.gap_start(previous_offered))
            self._drop_audio_before(capture_time)

        if self._writer is not None and getattr(self._writer, "failed", False) and self.encoder is not None:
            self._replace_failed_writer(frame)

        repeats = timeline.advance(capture_time, previous_offered)
        if (repeats > 0 and frame is not self._previous_frame
                and self._writer is not None and self._writer.isOpened()):
            # The image belongs in the last slot it reached; the previous image stays on
            # screen through any slots before it (stored once, not repeated). An unchanged
            # image (the same frame again) needs no write at all; release() sets the end.
            self._writer.write(frame, timeline.frames_written - 1)
        self._previous_frame = frame

        self._flush_audio(capture_time)

    def _replace_failed_writer(self, frame: np.ndarray) -> None:
        """
        The ffmpeg encoder exited. If it never produced anything (it couldn't open on this
        system), the file is restarted with the next working encoder, or OpenCV. Its frame
        count is restored with the held image, so the timeline and audio stay in sync.
        """
        old_writer = self._writer
        old_writer.release()  # Reports the encoder as failed if it produced nothing
        if old_writer.produced_output():
            self.encoder = None  # Failed partway (e.g. disk full): what was encoded is kept as it is
            return
        self._temp_video.unlink(missing_ok=True)
        self.encoder = video_encoder(self.dimensions, self.fps)
        self._writer = open_video_writer(self._temp_video, self.fourcc_str, self.fps, self.dimensions, self.encoder,
                                         background=self.background)
        if self._timeline.frames_written:
            # The slots already used keep the held image, so later frames stay in sync.
            held_frame = self._previous_frame if self._previous_frame is not None else frame
            self._writer.write(held_frame, 0)
        name = self.encoder[0] if self.encoder is not None else "OpenCV"
        print(f"{self.output_path.name}: continuing with {name}.")

    def discard_idle_audio(self, now: float) -> None:
        """Keeps queued audio from growing while no frames are arriving (e.g. during a pause)."""
        timeline = self._timeline
        if timeline.first_ts is None:
            self._drop_audio_before(now - config.AV_GAP_CUT_SECONDS)
        elif now - timeline.last_ts > config.AV_GAP_CUT_SECONDS:
            self._flush_audio(timeline.last_ts)
            self._drop_audio_before(now)

    def finish(self) -> None:
        """Closes the output files and moves the finished recording into place."""
        if self._timeline.last_ts is not None:
            self._flush_audio(self._timeline.last_ts)

        if self._writer is not None:
            self._writer.release(self.frames_written)  # The file lasts exactly the timeline's length
            self.encode_failed = getattr(self._writer, "failed", False)
            self._writer = None

        tracks = self._close_audio_tracks()

        if self.encode_failed:
            if not self.keep_partial or not self._temp_video.exists():
                # The caller can retry with OpenCV; nothing is moved into place.
                self._temp_video.unlink(missing_ok=True)
                self._remove_track_files(tracks)
                return
            print(f"Warning: the encoder stopped early; keeping what was recorded of {self.output_path.name}.")

        if not self._temp_video.exists():
            print(f"Error: no video was written for {self.output_path.name}.")
            self._remove_track_files(tracks)
            return

        if tracks and mux_audio_video(self._temp_video, tracks, self.output_path):
            self._temp_video.unlink(missing_ok=True)
            self._remove_track_files(tracks)
            return

        os.replace(self._temp_video, self.output_path)
        if tracks:
            print(f"Saved {self.output_path.name} without audio. Audio kept at: {[str(p) for p, _ in tracks]}")

    def _track_writer(self, track: str) -> Optional[AudioTrackWriter]:
        """The writer for a track, created when the track's first chunk arrives."""
        writer = self._audio_tracks.get(track)
        if writer is None and self.audio_format is not None and track not in self._failed_tracks:
            path = self._temp_dir / f"{self.output_path.stem}.audio.{track_slug(track)}.wav"
            try:
                writer = AudioTrackWriter(path, self.audio_format)
            except OSError as e:
                print(f"Warning: could not create audio track {track_label(track)}: {e}")
                self._failed_tracks.add(track)
                return None
            self._audio_tracks[track] = writer
        return writer

    def _flush_audio(self, up_to: float) -> None:
        """Writes queued audio captured at or before the given time."""
        while self._pending_audio and self._pending_audio[0][0] <= up_to:
            capture_time, track, samples = self._pending_audio.popleft()
            writer = self._track_writer(track)
            if writer is None:
                continue
            media_start = self._timeline.media_time(capture_time)
            try:
                writer.add_chunk(media_start, samples)
            except OSError as e:
                # A failed audio write (e.g. disk full) must not lose the video as well.
                print(f"Warning: audio write failed for {track_label(track)}: {e}. Dropping that track.")
                writer.close()
                del self._audio_tracks[track]
                self._failed_tracks.add(track)
                if writer.path is not None:
                    writer.path.unlink(missing_ok=True)

    def _drop_audio_before(self, capture_time: float) -> None:
        while self._pending_audio and self._pending_audio[0][0] < capture_time:
            self._pending_audio.popleft()

    def _close_audio_tracks(self) -> List[AudioTrackFile]:
        """Pads and closes every track, returning those with audio to mux (in output order)."""
        tracks: List[AudioTrackFile] = []
        for track in sorted(self._audio_tracks, key=track_sort_key):
            writer = self._audio_tracks[track]
            try:
                writer.pad_to_duration(self.frames_written / self.fps)
                writer.close()
            except OSError as e:
                print(f"Warning: could not finish audio track {track_label(track)}: {e}.")
                writer.path.unlink(missing_ok=True)
                continue
            tracks.append((writer.path, track))
        self._audio_tracks.clear()
        if self.frames_written == 0:
            self._remove_track_files(tracks)
            return []
        return tracks

    @staticmethod
    def _remove_track_files(tracks: List[AudioTrackFile]) -> None:
        for path, _ in tracks:
            path.unlink(missing_ok=True)


class AsyncVideoWriter:
    """
    High-performance asynchronous VideoWriter that offloads video encoding, audio
    placement, muxing and disk I/O to a dedicated background worker thread.
    Prevents the screen capture and GUI loop from stalling during video compression.
    """

    def __init__(
        self,
        output_path: Union[str, Path],
        fourcc_str: str,
        fps: float,
        dimensions: Tuple[int, int],
        queue_size: Optional[int] = None,
        audio_source=None,
    ):
        """
        queue_size: frames waiting for the encoder (default: half a second's worth). A short
        queue keeps the encoder close to real time: if it can't keep up, frames are skipped
        evenly (each one held until the next, correctly timed), rather than piling up
        seconds of backlog that then has to be dropped all at once (a long freeze).
        """
        self.output_path = Path(output_path)
        self.fourcc_str = fourcc_str
        self.fps = fps
        self.dimensions = dimensions
        if queue_size is None:
            queue_size = max(8, int(round(fps * WRITER_QUEUE_SECONDS)))
        self.queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self.running = True
        self._frames_written = 0
        self.dropped_frames = 0
        self._last_offered: Optional[float] = None
        self._audio_source = audio_source

        audio_format = audio_source.audio_format if audio_source is not None else None
        # The encoder (and the quality's bitrate) is picked on the worker thread.
        self._assembler = AVAssembler(output_path, fourcc_str, fps, dimensions, audio_format,
                                      encoder=AUTO_ENCODER, keep_partial=True)
        if audio_format is not None:
            audio_source.add_sink(self._assembler.add_audio)

        # Non-daemon so that closing the app cannot kill the encode or mux partway through.
        self._thread = threading.Thread(target=self._worker, daemon=False)
        self._thread.start()

    def _worker(self) -> None:
        """Background thread executing the actual encoding and muxing calls."""
        main_thread = threading.main_thread()
        try:
            self._assembler.open()
            # The main-thread check stops this thread keeping the process alive
            # if the app exits without calling release().
            while (self.running and main_thread.is_alive()) or not self.queue.empty():
                try:
                    capture_time, frame, previous_offered = self.queue.get(timeout=0.03)
                except queue.Empty:
                    self._assembler.discard_idle_audio(time.perf_counter())
                    continue
                self._assembler.add_frame(capture_time, frame, previous_offered)
                self._frames_written = self._assembler.frames_written
                self.queue.task_done()
        finally:
            if self._audio_source is not None:
                self._audio_source.remove_sink(self._assembler.add_audio)
            self._assembler.finish()

    def write(self, frame: np.ndarray, capture_time: Optional[float] = None) -> None:
        """Pushes a frame into the background worker queue. Non-blocking.
        capture_time: when the image was on screen (perf_counter clock); default now."""
        if not self.running:
            return
        now = capture_time if capture_time is not None else time.perf_counter()
        previous_offered, self._last_offered = self._last_offered, now
        try:
            self.queue.put_nowait((now, frame, previous_offered))
        except queue.Full:
            # Never wait here: blocking would stall capture for every consumer. A dropped frame
            # is covered by the timeline (the previous image is held), so the file stays in sync,
            # and the next frame carries this one's time so the gap isn't mistaken for a pause.
            self.dropped_frames += 1

    def release(self) -> None:
        """Signals the worker thread to finish remaining queued frames and release resources."""
        self.running = False
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)
        if self.dropped_frames:
            print(f"Note: the encoder couldn't keep up; {self.dropped_frames} frames of {self.output_path.name} "
                  f"were skipped (the previous image held; timing and sync are unaffected). "
                  f"A lower resolution or frame rate gives smoother video.")

    @property
    def frames_written(self) -> int:
        return self._frames_written


def write_frames_to_file(
    output_path: Union[str, Path],
    frames: Iterable[np.ndarray],
    fourcc_str: str,
    fps: float,
    dimensions: Tuple[int, int],
) -> None:
    """Writes a sequence of frames directly to a file and releases the writer."""
    writer = create_video_writer(output_path, fourcc_str, fps, dimensions)
    try:
        for frame in frames:
            writer.write(frame)
    finally:
        writer.release()


def write_av_clip(
    output_path: Union[str, Path],
    frames: Sequence[TimedFrame],
    audio_chunks: List[AudioChunk],
    audio_format: Optional[AudioFormat],
    fourcc_str: str,
    fps: float,
    dimensions: Tuple[int, int],
) -> None:
    """
    Writes timestamped frames and audio chunks to a single synchronized file.
    Uses the configured ffmpeg encoder and quality when available, falling back to OpenCV.
    """
    encoder = video_encoder(dimensions, fps)
    while True:
        assembler = AVAssembler(output_path, fourcc_str, fps, dimensions, audio_format, encoder, background=True)
        assembler.open()
        try:
            for capture_time, track, samples in audio_chunks:
                assembler.add_audio(track, capture_time, samples)
            for capture_time, frame in frames:
                assembler.add_frame(capture_time, frame)
        finally:
            assembler.finish()
        if not assembler.encode_failed or assembler.encoder is None:
            return
        failed_name = assembler.encoder[0]
        # The failed encoder was reported if it produced nothing, so this picks the next one.
        encoder = video_encoder(dimensions, fps)
        if encoder is not None and encoder[0] == failed_name:
            encoder = None
        print(f"Warning: {failed_name} could not encode the clip. Retrying with "
              f"{encoder[0] if encoder is not None else 'OpenCV'}.")
