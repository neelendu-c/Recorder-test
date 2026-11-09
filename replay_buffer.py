"""
Instant replay buffers for saving retrospective clips with their audio tracks.

Two tiers, chosen by how much memory the clip duration would need as raw frames:
- ReplayBuffer (RAM tier): raw frames in a circular buffer. Zero encoding cost.
- DiskReplayRing (disk tier): frames are encoded continuously into short rolling segment
  files, so clips of up to 60 minutes need only disk space. Exports are frame-exact and
  sample-exact: the partial segments at each end are re-encoded, the rest are joined
  with ffmpeg's stream copy, and each audio track is copied by sample position.
"""
from collections import deque
import itertools
import math
import os
from pathlib import Path
import queue
import shutil
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple, Union
import wave

import cv2
import numpy as np

import config
import win32_api
from video_writer import (
    AudioTrackFile,
    AudioTrackWriter,
    Encoder,
    FrameTimeline,
    generate_timestamped_filename,
    lower_current_thread_priority,
    mux_audio_video,
    open_video_writer,
    open_wav,
    run_ffmpeg,
    track_label,
    track_slug,
    track_sort_key,
    video_encoder,
    write_av_clip,
)

# Audio kept from before the oldest buffered frame, so a clip's start is always covered.
AUDIO_RETENTION_MARGIN_SECONDS = 1.0
# Disk tier: how long to wait for more audio after a segment's video closes before
# finishing its audio with silence (loopback delivers nothing while the system is silent).
AUDIO_SEGMENT_SETTLE_SECONDS = 0.25
DISK_RING_QUEUE_SIZE = 60

_ring_ids = itertools.count(1)


# ----------------------------------------------------------------------------
# Tier selection
# ----------------------------------------------------------------------------

def ram_budget_bytes() -> int:
    """Raw-frame memory the RAM tier may use (REPLAY_RAM_BUDGET_MB, or 25% of installed RAM)."""
    if config.REPLAY_RAM_BUDGET_MB is not None:
        return int(config.REPLAY_RAM_BUDGET_MB) * 1024 * 1024
    total = win32_api.total_physical_memory_bytes()
    return total // 4 if total else 2 * 1024 ** 3


def estimated_raw_bytes(dimensions: Tuple[int, int], fps: int, seconds: int) -> int:
    width, height = dimensions
    return width * height * 3 * int(fps) * int(seconds)


def select_tier(dimensions: Tuple[int, int], fps: int, seconds: int) -> str:
    return "ram" if estimated_raw_bytes(dimensions, fps, seconds) <= ram_budget_bytes() else "disk"


def create_replay_buffer(tier: str, dimensions: Tuple[int, int], fps: int, seconds: int, audio_source, fourcc: str):
    if tier == "ram":
        return ReplayBuffer(max_frames=int(fps * seconds), dimensions=dimensions, audio_source=audio_source, fps=fps)
    return DiskReplayRing(seconds, fps, dimensions, fourcc, audio_source)


def remove_stale_ring_files() -> None:
    """Deletes disk-tier segments left behind by a previous run."""
    shutil.rmtree(config.REPLAY_RING_DIR, ignore_errors=True)


# ----------------------------------------------------------------------------
# RAM tier
# ----------------------------------------------------------------------------

class ReplayBuffer:
    """Manages a rolling circular buffer of recent frames and audio for instant replay clipping."""

    tier = "ram"

    def __init__(self, max_frames: int, dimensions: Tuple[int, int] = (0, 0), audio_source=None,
                 fps: Optional[float] = None):
        """
        max_frames: the clip length in frames at fps. Frames arrive only when the screen
        changes (at most one per frame slot; a still screen sends one every 0.1 s), so the
        buffer is "full" once it spans the clip's duration, however many frames that took.
        """
        self.max_frames = max_frames
        self.fps = fps
        self.dimensions = dimensions
        self._buffer: deque = deque(maxlen=max_frames)  # (capture_time, frame)
        self._audio_source = audio_source
        self._audio_chunks: deque = deque()  # (capture_time, track, samples)
        self._audio_lock = threading.Lock()

        if audio_source is not None:
            audio_source.add_sink(self._add_audio_chunk)

    def _add_audio_chunk(self, track: str, capture_time: float, samples: np.ndarray) -> None:
        """Stores a captured audio chunk. Called from the audio threads."""
        with self._audio_lock:
            self._audio_chunks.append((capture_time, track, samples))

    def add_frame(self, frame: np.ndarray, capture_time: Optional[float] = None) -> None:
        """Stores the given frame into the circular buffer and trims audio to match.
        capture_time: when the image was on screen (perf_counter clock); default now."""
        self._buffer.append((capture_time if capture_time is not None else time.perf_counter(), frame))

        keep_from = self._buffer[0][0] - AUDIO_RETENTION_MARGIN_SECONDS
        with self._audio_lock:
            while self._audio_chunks and self._audio_chunks[0][0] < keep_from:
                self._audio_chunks.popleft()

    def resize(self, new_max_frames: int, fps: Optional[float] = None) -> None:
        """Dynamically adjusts the maximum capacity of the circular buffer while preserving recent frames."""
        self.max_frames = new_max_frames
        if fps is not None:
            self.fps = fps
        self._buffer = deque(self._buffer, maxlen=new_max_frames)

    def _span_seconds(self) -> float:
        buffer = self._buffer
        if len(buffer) < 2:
            return 0.0
        return buffer[-1][0] - buffer[0][0]

    def is_full(self) -> bool:
        """True once the buffer covers the whole clip duration."""
        if not self.fps:
            return len(self._buffer) == self._buffer.maxlen
        if len(self._buffer) == self._buffer.maxlen:
            return True
        return self._span_seconds() + 1.0 / self.fps >= self.max_frames / self.fps

    def get_frames(self) -> List[np.ndarray]:
        """Returns a list snapshot of the currently buffered frames."""
        return [frame for _, frame in self._buffer]

    def take_warning(self) -> Optional[str]:
        return None

    def close(self) -> None:
        """Stops receiving audio and frees the buffered frames."""
        if self._audio_source is not None:
            self._audio_source.remove_sink(self._add_audio_chunk)
        self._buffer.clear()
        with self._audio_lock:
            self._audio_chunks.clear()

    def _snapshot_clip(self, fps: float) -> Tuple[list, list]:
        """Returns the frames and audio chunks covering the last max_frames / fps seconds."""
        timed_frames = list(self._buffer)
        clip_start = timed_frames[-1][0] - self.max_frames / fps
        inside = [item for item in timed_frames if item[0] >= clip_start]
        before = [item for item in timed_frames if item[0] < clip_start]
        if before and (not inside or inside[0][0] > clip_start):
            # The image on screen when the clip starts arrived earlier (a still screen sends
            # few frames): it opens the clip, so the clip has its full length.
            inside.insert(0, (clip_start, before[-1][1]))
        timed_frames = inside

        with self._audio_lock:
            audio_chunks = list(self._audio_chunks)
        return timed_frames, audio_chunks

    def _audio_format(self) -> Optional[Tuple[int, int]]:
        return self._audio_source.audio_format if self._audio_source is not None else None

    def save_clip(
        self,
        output_dir: Union[str, Path],
        fps: float,
        dimensions: Tuple[int, int],
        fourcc_str: str,
        prefix: str = "clipped_30s",
    ) -> Optional[Path]:
        """
        Exports the current buffer content synchronously as a video file.

        :return: Path to the saved clip file, or None if buffer is empty or not full.
        """
        if not self.is_full():
            print("Buffer is not yet full. Cannot save complete clip.")
            return None

        filename = generate_timestamped_filename(prefix=prefix, extension=".mp4")
        save_path = Path(output_dir) / filename

        timed_frames, audio_chunks = self._snapshot_clip(fps)
        write_av_clip(save_path, timed_frames, audio_chunks, self._audio_format(), fourcc_str, fps, dimensions)
        return save_path

    def save_clip_async(
        self,
        output_dir: Union[str, Path],
        fps: float,
        dimensions: Tuple[int, int],
        fourcc_str: str,
        prefix: str = "clipped_30s",
        on_complete: Optional[Callable[[Path], None]] = None,
    ) -> Optional[Path]:
        """
        Exports the current buffer content asynchronously in a background worker thread
        so that live recording never drops frames during disk encoding.

        :return: Path to the anticipated clip file, or None if buffer is not full.
        """
        if not self.is_full():
            print("Buffer is not yet full. Cannot save complete clip.")
            return None

        filename = generate_timestamped_filename(prefix=prefix, extension=".mp4")
        save_path = Path(output_dir) / filename
        timed_frames, audio_chunks = self._snapshot_clip(fps)
        audio_format = self._audio_format()

        def _worker():
            lower_current_thread_priority()
            try:
                write_av_clip(save_path, timed_frames, audio_chunks, audio_format, fourcc_str, fps, dimensions)
                if on_complete:
                    on_complete(save_path)
            except Exception as e:
                print(f"Error asynchronously writing clip: {e}")

        # Non-daemon so that closing the app cannot kill an export partway through.
        threading.Thread(target=_worker, daemon=False).start()
        return save_path

    def __len__(self) -> int:
        """How much of the clip is buffered, in frames at fps (what the dashboard gauge shows)."""
        if not self.fps:
            return len(self._buffer)
        if self.is_full():
            return self.max_frames
        return min(self.max_frames, int(round(self._span_seconds() * self.fps)) + (1 if self._buffer else 0))


# ----------------------------------------------------------------------------
# Disk tier
# ----------------------------------------------------------------------------

class _SegmentLayout:
    """Frame and sample boundaries shared by the ring writer and its exports."""

    def __init__(self, directory: Path, segment_frames: int, fps: float, audio_format: Optional[Tuple[int, int]]):
        self.directory = directory
        self.segment_frames = segment_frames
        self.fps = fps
        self.sample_rate = audio_format[0] if audio_format else 0

    def video_path(self, index: int) -> Path:
        return self.directory / f"seg_{index:06d}.mp4"

    def audio_path(self, index: int, track: str) -> Path:
        return self.directory / f"seg_{index:06d}.{track_slug(track)}.wav"

    def sample_at_frame(self, frame_index: int) -> int:
        return round(frame_index * self.sample_rate / self.fps)

    def segment_start_sample(self, index: int) -> int:
        return self.sample_at_frame(index * self.segment_frames)

    def segment_for_sample(self, sample: int) -> int:
        index = int(sample * self.fps // (self.segment_frames * self.sample_rate))
        while index > 0 and self.segment_start_sample(index) > sample:
            index -= 1
        while self.segment_start_sample(index + 1) <= sample:
            index += 1
        return index


class _RingAudioWriter(AudioTrackWriter):
    """Places one track on the ring's timeline and splits it into per-segment WAV files at exact sample boundaries."""

    max_bytes = None  # The ring is unbounded overall; each segment file is small.

    def __init__(self, layout: _SegmentLayout, audio_format: Tuple[int, int], track: str, origin: int,
                 on_segment_closed: Callable[[str, int], None]):
        # A track that appears mid-stream starts at the current segment, not at sample 0.
        super().__init__(None, audio_format, origin=origin)
        self.track = track
        self.first_index = layout.segment_for_sample(origin)
        self._layout = layout
        self._audio_format = audio_format
        self._on_segment_closed = on_segment_closed
        self._segment_wav = None
        self._segment_index = -1

    def _emit(self, data: bytes) -> None:
        bytes_per_frame = self._bytes_per_frame
        total = len(data) // bytes_per_frame
        position = self.frames_written  # Sample index of data[0]; updated by the caller afterwards
        offset = 0
        while offset < total:
            sample = position + offset
            if self._segment_wav is None:
                self._segment_index = self._layout.segment_for_sample(sample)
                self._segment_wav = open_wav(self._layout.audio_path(self._segment_index, self.track),
                                             self._audio_format)
            segment_end = self._layout.segment_start_sample(self._segment_index + 1)
            count = min(total - offset, segment_end - sample)
            self._segment_wav.writeframesraw(data[offset * bytes_per_frame:(offset + count) * bytes_per_frame])
            offset += count
            if sample + count >= segment_end:
                self._close_segment()

    def _close_segment(self) -> None:
        if self._segment_wav is not None:
            self._segment_wav.close()
            self._segment_wav = None
            self._on_segment_closed(self.track, self._segment_index)

    def close(self) -> None:
        self._close_segment()


class DiskReplayRing:
    """Encodes the replay stream into rolling segment files on a background thread."""

    tier = "disk"

    def __init__(self, clip_seconds: int, fps: int, dimensions: Tuple[int, int], fourcc_str: str, audio_source=None):
        self.fps = fps
        self.dimensions = dimensions
        self.fourcc_str = fourcc_str
        self.max_frames = int(round(fps * clip_seconds))
        self.audio_format = audio_source.audio_format if audio_source is not None else None
        self._audio_source = audio_source if self.audio_format is not None else None
        # Chosen on the ring thread (the first choice may wait for the encoder probe). Every
        # segment uses the same encoder settings, so they can be joined without re-encoding.
        self._encoder: Optional[Encoder] = None

        directory = Path(config.REPLAY_RING_DIR) / f"ring_{os.getpid()}_{next(_ring_ids)}"
        directory.mkdir(parents=True, exist_ok=True)
        segment_frames = max(1, round(config.REPLAY_SEGMENT_SECONDS * fps))
        self._layout = _SegmentLayout(directory, segment_frames, fps, self.audio_format)

        self._timeline = FrameTimeline(fps)
        self._queue: queue.Queue = queue.Queue(maxsize=DISK_RING_QUEUE_SIZE)
        self._last_offered: Optional[float] = None
        self._requests: queue.Queue = queue.Queue()
        self._pending_audio: deque = deque()
        self._previous_frame: Optional[np.ndarray] = None
        self._writer = None
        self._writer_index: Optional[int] = None
        self._spare: Optional[Tuple[int, threading.Thread, List]] = None  # Next segment's writer, opened early
        self._resume_index = 0  # Segments before this are skipped after an encoder failure
        self._failed_segment = -1  # Highest segment whose encoder failed when it was closed
        self._closers: List[threading.Thread] = []

        self._lock = threading.Lock()
        self._video_closed: Set[int] = set()
        self._video_close_times: Dict[int, float] = {}
        self._audio_writers: Dict[str, _RingAudioWriter] = {}
        self._audio_closed: Dict[str, Set[int]] = {}
        self._failed_tracks: Set[str] = set()
        self._tracks_seen: Set[str] = set()
        self._pins: Dict[int, int] = {}
        self._oldest_index = 0
        self._stopped = False
        self._space_checked = False
        self._warning: Optional[str] = None

        if self._audio_source is not None:
            self._audio_source.add_sink(self._audio_sink)

        self.running = True
        # Non-daemon so that closing the app cannot kill the encoder partway through a segment.
        self._thread = threading.Thread(target=self._worker, daemon=False, name="DiskReplayRing")
        self._thread.start()

    def _audio_sink(self, track: str, capture_time: float, samples) -> None:
        """Audio-thread sink: stores the chunk for the ring thread. deque.append is thread-safe."""
        self._pending_audio.append((capture_time, track, samples))

    # --- Main-thread interface ------------------------------------------------------

    def add_frame(self, frame: np.ndarray, capture_time: Optional[float] = None) -> None:
        """Queues a frame for encoding. Never blocks; a dropped frame is covered by the timeline.
        capture_time: when the image was on screen (perf_counter clock); default now."""
        now = capture_time if capture_time is not None else time.perf_counter()
        previous_offered, self._last_offered = self._last_offered, now
        try:
            self._queue.put_nowait((now, frame, previous_offered))
        except queue.Full:
            pass  # The next frame carries this time, so the drop isn't mistaken for a pause

    def set_duration(self, clip_seconds: int) -> None:
        """Changes the clip length. Retention grows or shrinks from here on."""
        self.max_frames = int(round(self.fps * clip_seconds))

    def available_frames(self) -> int:
        return max(0, self._timeline.frames_written - self._oldest_index * self._layout.segment_frames)

    def is_full(self) -> bool:
        return self.available_frames() >= self.max_frames

    def __len__(self) -> int:
        return min(self.available_frames(), self.max_frames)

    def take_warning(self) -> Optional[str]:
        warning, self._warning = self._warning, None
        return warning

    def save_clip_async(
        self,
        output_dir: Union[str, Path],
        fps: float,
        dimensions: Tuple[int, int],
        fourcc_str: str,
        prefix: str = "clipped_30s",
        on_complete: Optional[Callable[[Path], None]] = None,
    ) -> Optional[Path]:
        """Requests an exact-length clip of the last max_frames frames. Returns the planned path."""
        if not self.is_full():
            print("Buffer is not yet full. Cannot save complete clip.")
            return None
        save_path = Path(output_dir) / generate_timestamped_filename(prefix=prefix, extension=".mp4")
        # The ring thread picks the end frame, so it matches what it has encoded.
        self._requests.put((save_path, on_complete))
        return save_path

    def close(self) -> None:
        """Stops encoding. Segments still being exported are deleted once their export finishes."""
        self.running = False
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

    # --- Ring thread ----------------------------------------------------------------

    def _worker(self) -> None:
        main_thread = threading.main_thread()
        try:
            self._encoder = video_encoder(self.dimensions, self.fps)
            while (self.running and main_thread.is_alive()) or not self._queue.empty():
                try:
                    capture_time, frame, previous_offered = self._queue.get(timeout=0.03)
                except queue.Empty:
                    self._discard_idle_audio(time.perf_counter())
                else:
                    self._add_frame(capture_time, frame, previous_offered)
                self._check_closed_failures()
                self._process_requests()
                self._settle_silent_audio(time.perf_counter())
                self._enforce_retention()
        except Exception as e:
            print(f"Error in disk replay buffer: {e}")
        finally:
            if self._audio_source is not None:
                self._audio_source.remove_sink(self._audio_sink)
            if self._writer_index is not None:
                # The open segment ends where the timeline does (it isn't full).
                partial = self._timeline.frames_written - self._writer_index * self._layout.segment_frames
                self._close_video_segment(time.perf_counter(), end_slot=max(1, partial))
            self._discard_spare()
            for closer in self._closers:
                closer.join(timeout=10.0)
            for writer in list(self._audio_writers.values()):
                try:
                    writer.close()
                except OSError:
                    pass
            with self._lock:
                self._stopped = True
                no_pins = not any(self._pins.values())
            if no_pins:
                shutil.rmtree(self._layout.directory, ignore_errors=True)

    def _add_frame(self, capture_time: float, frame: np.ndarray, previous_offered: Optional[float]) -> None:
        timeline = self._timeline
        if timeline.is_gap(capture_time, previous_offered):
            # Audio from before the gap (including while frames were being dropped) is kept.
            self._flush_audio(timeline.gap_start(previous_offered))
            self._drop_audio_before(capture_time)

        first_slot = timeline.frames_written
        repeats = timeline.advance(capture_time, previous_offered)
        if repeats > 0:
            held_frame = self._previous_frame if self._previous_frame is not None else frame
            self._write_video_slots(first_slot, first_slot + repeats - 1, held_frame, frame, capture_time)
        self._previous_frame = frame
        self._flush_audio(capture_time)

    def _write_video_slots(self, first: int, last: int, held: np.ndarray, frame: np.ndarray,
                           capture_time: float) -> None:
        """
        Places a frame at slot `last`; slots first..last-1 still show the held image.
        Writers store each image once at its slot (see video_writer), so only segment starts
        need the held image written again: every segment file must begin with what was on
        screen at its first slot, so it plays (and joins) correctly on its own.
        """
        segment_frames = self._layout.segment_frames
        slot = first
        while slot <= last:
            index = slot // segment_frames
            segment_start = index * segment_frames
            segment_end = segment_start + segment_frames
            if index < self._resume_index:
                slot = segment_end  # A segment whose encoder failed; it's never exported
                continue
            wrote_frame = False
            if index != self._writer_index:
                # The previous segment is complete: everything up to its end is known now.
                self._close_video_segment(capture_time)
                self._writer = self._take_writer(index)
                self._writer_index = index
                wrote_frame = slot == last
                self._writer.write(frame if wrote_frame else held, slot - segment_start)
                if self._writer_failed(index):
                    return
            if last < segment_end:
                if not wrote_frame and frame is not held:  # An unchanged image needs no write
                    self._writer.write(frame, last - segment_start)
                    if self._writer_failed(index):
                        return
                if last - segment_start >= segment_frames // 2:
                    self._prepare_writer(index + 1)  # Ready before this segment ends
                return
            slot = segment_end  # The held image lasts to this segment's end; the next starts with it

    def _writer_failed(self, index: int) -> bool:
        if getattr(self._writer, "failed", False):
            self._on_encoder_failure(index)
            return True
        return False

    def _on_encoder_failure(self, index: int) -> None:
        """
        The segment's ffmpeg encoder exited. Its file is unusable, and segments from another
        encoder can't be joined with later ones, so the ring restarts at the next segment
        with the next working encoder (or OpenCV). Clips need the buffer to refill.
        """
        writer, self._writer, self._writer_index = self._writer, None, None
        writer.release()  # Reports the encoder as failed if it produced nothing
        self._discard_spare()  # Prepared with the failed encoder
        self._invalidate_through(index)
        self._resume_index = index + 1
        self._encoder = video_encoder(self.dimensions, self.fps)
        name = self._encoder[0] if self._encoder is not None else "OpenCV"
        self._warning = f"The replay encoder failed; the buffer restarted with {name} and needs to refill."

    def _check_closed_failures(self) -> None:
        """Handles a segment whose encoder only failed when it was finished (on a closer thread)."""
        with self._lock:
            failed, self._failed_segment = self._failed_segment, -1
        if failed < self._resume_index:
            return
        if self._writer is not None:
            # The open segment uses the same encoder, so it's dropped too.
            failed = max(failed, self._writer_index)
            writer, self._writer, self._writer_index = self._writer, None, None
            writer.abort()
        # (release() on the closer thread already reported the encoder if it produced nothing.)
        self._discard_spare()
        self._invalidate_through(failed)
        self._resume_index = failed + 1
        self._encoder = video_encoder(self.dimensions, self.fps)
        name = self._encoder[0] if self._encoder is not None else "OpenCV"
        self._warning = f"The replay encoder failed; the buffer restarted with {name} and needs to refill."

    def _invalidate_through(self, index: int) -> None:
        """Drops every segment up to and including index from the ring (no clip will use them)."""
        with self._lock:
            first = self._oldest_index
            if index < first:
                return
            for i in range(first, index + 1):
                self._video_closed.discard(i)
                self._video_close_times.pop(i, None)
                for closed in self._audio_closed.values():
                    closed.discard(i)
            self._oldest_index = index + 1
            removable = [i for i in range(first, index + 1) if not self._pins.get(i)]
        for i in removable:
            paths = [self._layout.video_path(i)] + [self._layout.audio_path(i, t) for t in list(self._tracks_seen)]
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass  # Still open (the current audio segment); removed with the ring directory

    def _open_segment_writer(self, index: int):
        return open_video_writer(self._layout.video_path(index), self.fourcc_str, self.fps,
                                 self.dimensions, self._encoder)

    def _prepare_writer(self, index: int) -> None:
        """Opens the next segment's file and encoder early, on the side, so the ring thread never
        waits for that at a segment boundary. The encoder itself starts only with the first
        frame, so a waiting writer uses no encoder session."""
        if self._spare is not None or self._encoder is None:
            return
        holder: List = []

        def _open():
            try:
                holder.append(self._open_segment_writer(index))
            except Exception as e:
                print(f"Warning: could not prepare the next replay segment: {e}")

        thread = threading.Thread(target=_open, daemon=True, name="RingSegmentOpen")
        thread.start()
        self._spare = (index, thread, holder)

    def _take_writer(self, index: int):
        spare, self._spare = self._spare, None
        if spare is not None:
            spare_index, thread, holder = spare
            thread.join()
            if holder and spare_index == index:
                return holder[0]
            if holder:
                self._discard_writer(holder[0], spare_index)
        return self._open_segment_writer(index)

    def _discard_writer(self, writer, index: int) -> None:
        writer.abort()
        self._layout.video_path(index).unlink(missing_ok=True)

    def _discard_spare(self) -> None:
        spare, self._spare = self._spare, None
        if spare is not None:
            spare_index, thread, holder = spare
            thread.join(timeout=5.0)
            if holder:
                self._discard_writer(holder[0], spare_index)

    def _close_video_segment(self, capture_time: float, end_slot: Optional[int] = None) -> None:
        """Finishes the open segment. It ends at its full length unless end_slot (relative to
        the segment's start) says otherwise (the partial last segment when the ring stops)."""
        if self._writer is None:
            return
        writer, index = self._writer, self._writer_index
        self._writer = None
        self._writer_index = None
        if end_slot is None:
            end_slot = self._layout.segment_frames
        # Flushing the encoder would stall the ring thread, so it's done aside.
        self._closers = [t for t in self._closers if t.is_alive()]
        closer = threading.Thread(target=self._finish_video_segment, args=(writer, index, capture_time, end_slot),
                                  daemon=False, name="RingSegmentClose")
        closer.start()
        self._closers.append(closer)

    def _finish_video_segment(self, writer, index: int, capture_time: float, end_slot: int) -> None:
        writer.release(end_slot)
        if getattr(writer, "failed", False):
            # Runs on a closer thread; the ring thread switches encoders (_check_closed_failures).
            with self._lock:
                self._failed_segment = max(self._failed_segment, index)
            return
        with self._lock:
            self._video_closed.add(index)
            self._video_close_times[index] = capture_time
        if not self._space_checked:
            self._space_checked = True
            self._check_disk_space(index)

    def _ring_track(self, track: str) -> Optional[_RingAudioWriter]:
        writer = self._audio_writers.get(track)
        if writer is None and self.audio_format is not None and track not in self._failed_tracks:
            current_segment = self._timeline.frames_written // self._layout.segment_frames
            origin = self._layout.segment_start_sample(current_segment)
            writer = _RingAudioWriter(self._layout, self.audio_format, track, origin, self._mark_audio_closed)
            with self._lock:
                self._audio_writers[track] = writer
                self._audio_closed[track] = set()
            self._tracks_seen.add(track)
        return writer

    def _drop_track(self, track: str, error: Exception) -> None:
        print(f"Warning: replay audio write failed for {track_label(track)}: {error}.")
        self._warning = f"Replay audio ({track_label(track)}) write failed. New clips won't include it."
        with self._lock:
            writer = self._audio_writers.pop(track, None)
            self._audio_closed.pop(track, None)
            self._failed_tracks.add(track)
        if writer is not None:
            try:
                writer.close()
            except OSError:
                pass

    def _mark_audio_closed(self, track: str, index: int) -> None:
        with self._lock:
            closed = self._audio_closed.get(track)
            if closed is not None:
                closed.add(index)

    def _audio_done_locked(self, index: int) -> bool:
        """Every track that existed at segment `index` has finished it. Call with the lock held."""
        return all(
            index in self._audio_closed.get(track, ()) or writer.first_index > index
            for track, writer in self._audio_writers.items()
        )

    def _is_complete(self, index: int) -> bool:
        with self._lock:
            if index not in self._video_closed:
                return False
            # Once the ring has stopped no more audio will come; the export pads any shortfall.
            return self._stopped or self._audio_done_locked(index)

    def _flush_audio(self, up_to: float) -> None:
        while self._pending_audio and self._pending_audio[0][0] <= up_to:
            capture_time, track, samples = self._pending_audio.popleft()
            writer = self._ring_track(track)
            if writer is None:
                continue
            try:
                writer.add_chunk(self._timeline.media_time(capture_time), samples)
            except OSError as e:
                self._drop_track(track, e)

    def _drop_audio_before(self, capture_time: float) -> None:
        while self._pending_audio and self._pending_audio[0][0] < capture_time:
            self._pending_audio.popleft()

    def _discard_idle_audio(self, now: float) -> None:
        timeline = self._timeline
        if timeline.first_ts is None:
            self._drop_audio_before(now - config.AV_GAP_CUT_SECONDS)
        elif now - timeline.last_ts > config.AV_GAP_CUT_SECONDS:
            self._flush_audio(timeline.last_ts)
            self._drop_audio_before(now)

    def _settle_silent_audio(self, now: float) -> None:
        """Finishes a segment's audio with silence once no more audio for it can arrive."""
        for track, writer in list(self._audio_writers.items()):
            while True:
                index = self._layout.segment_for_sample(writer.frames_written)
                with self._lock:
                    close_time = self._video_close_times.get(index)
                    done = index in self._audio_closed.get(track, ())
                if done or close_time is None or now - close_time < AUDIO_SEGMENT_SETTLE_SECONDS:
                    break
                self._flush_audio(close_time)
                if track not in self._audio_writers:
                    break
                if self._layout.segment_for_sample(writer.frames_written) == index:
                    try:
                        writer.pad_to_samples(self._layout.segment_start_sample(index + 1))
                    except OSError as e:
                        self._drop_track(track, e)
                        break

    def _enforce_retention(self) -> None:
        segment_frames = self._layout.segment_frames
        current = self._timeline.frames_written // segment_frames
        keep_from = current - math.ceil(self.max_frames / segment_frames) - 1
        while self._oldest_index < keep_from:
            index = self._oldest_index
            with self._lock:
                if self._pins.get(index) or index not in self._video_closed or not self._audio_done_locked(index):
                    return
                self._video_closed.discard(index)
                for closed in self._audio_closed.values():
                    closed.discard(index)
                self._video_close_times.pop(index, None)
                self._oldest_index = index + 1
            self._layout.video_path(index).unlink(missing_ok=True)
            for track in self._tracks_seen:
                self._layout.audio_path(index, track).unlink(missing_ok=True)

    def _check_disk_space(self, index: int) -> None:
        try:
            segment_bytes = self._layout.video_path(index).stat().st_size
            for track in list(self._tracks_seen):
                audio_path = self._layout.audio_path(index, track)
                if audio_path.exists():
                    segment_bytes += audio_path.stat().st_size
            segments_needed = math.ceil(self.max_frames / self._layout.segment_frames) + 2
            needed = segment_bytes * segments_needed
            free = shutil.disk_usage(self._layout.directory).free
        except OSError:
            return
        if free < needed:
            self._warning = (f"Low disk space: the replay buffer needs about {needed / 1e9:.1f} GB "
                             f"but only {free / 1e9:.1f} GB is free.")

    def _process_requests(self) -> None:
        while True:
            try:
                save_path, on_complete = self._requests.get_nowait()
            except queue.Empty:
                return
            end_frame = self._timeline.frames_written
            start_frame = end_frame - self.max_frames
            segment_frames = self._layout.segment_frames
            if start_frame < self._oldest_index * segment_frames:
                print("Replay buffer doesn't hold enough footage for the clip yet.")
                continue
            first_index = start_frame // segment_frames
            last_index = (end_frame - 1) // segment_frames
            with self._lock:
                for index in range(first_index, last_index + 1):
                    self._pins[index] = self._pins.get(index, 0) + 1
            tracks = sorted(self._tracks_seen, key=track_sort_key)
            threading.Thread(
                target=self._export,
                args=(save_path, start_frame, end_frame, first_index, last_index, tracks, on_complete),
                daemon=False,
                name="ReplayExport",
            ).start()

    # --- Export thread --------------------------------------------------------------

    def _export(self, save_path: Path, start_frame: int, end_frame: int,
                first_index: int, last_index: int, tracks: List[str], on_complete) -> None:
        lower_current_thread_priority()
        try:
            # The last segment may still be recording; wait for it to close.
            deadline = time.perf_counter() + config.REPLAY_SEGMENT_SECONDS * 2 + 10
            while not self._is_complete(last_index):
                if first_index < self._oldest_index:
                    raise RuntimeError("the replay encoder failed and the buffer restarted; try again once it refills")
                if time.perf_counter() > deadline:
                    raise RuntimeError("timed out waiting for the replay buffer to finish a segment")
                time.sleep(0.05)
            export_ring_clip(
                self._layout, save_path, start_frame, end_frame,
                self.fourcc_str, self.fps, self.dimensions, self.audio_format, tracks, self._encoder,
            )
            if on_complete:
                on_complete(save_path)
        except Exception as e:
            print(f"Error exporting replay clip: {e}")
        finally:
            with self._lock:
                for index in range(first_index, last_index + 1):
                    self._pins[index] -= 1
                    if self._pins[index] <= 0:
                        del self._pins[index]
                delete_directory = self._stopped and not self._pins
            if delete_directory:
                shutil.rmtree(self._layout.directory, ignore_errors=True)


def _decode_slots(path: Path, fps: float):
    """
    Yields (slot, BGR image) for each image stored in a segment file, where slot is its
    frame position on the constant-rate timeline (from its timestamp). Segments store each
    distinct image once, so slots can skip; the image shown at slot s is the last one at or
    before s. Without PyAV, frames are read in order (one per slot), which is right for the
    constant-rate files written without it.
    """
    try:
        import av
    except ImportError:
        av = None
    if av is not None:
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            stream.thread_type = "AUTO"
            time_base = float(stream.time_base)
            start = stream.start_time or 0
            for frame in container.decode(stream):
                if frame.pts is None:
                    continue
                yield int(round((frame.pts - start) * time_base * fps)), frame.to_ndarray(format="bgr24")
        return
    capture = cv2.VideoCapture(str(path))
    try:
        slot = 0
        while True:
            ok, image = capture.read()
            if not ok:
                return
            yield slot, image
            slot += 1
    finally:
        capture.release()


def _reencode_frame_range(source: Path, destination: Path, first: int, last: int, fourcc_str: str,
                          fps: float, dimensions: Tuple[int, int], encoder: Optional[Encoder]) -> None:
    """Writes slots [first, last) of a segment to a new file with the same encoder settings."""
    writer = open_video_writer(destination, fourcc_str, fps, dimensions, encoder, background=True)
    try:
        on_screen = None  # The image shown at slot `first` (the last one at or before it)
        started = False
        for slot, image in _decode_slots(source, fps):
            if slot <= first:
                on_screen = image
                continue
            if slot >= last:
                break
            if not started:
                writer.write(on_screen if on_screen is not None else image, 0)
                started = True
            writer.write(image, slot - first)
        if not started:
            if on_screen is None:
                on_screen = np.zeros((dimensions[1], dimensions[0], 3), dtype=np.uint8)
            writer.write(on_screen, 0)
    finally:
        writer.release(last - first)  # Exactly the requested length


def _video_input_arguments(parts: List[Path], work_dir: Path) -> List[str]:
    """ffmpeg input arguments that read the parts as one stream (concat demuxer for several)."""
    if len(parts) == 1:
        return ["-i", str(parts[0])]
    list_file = work_dir / "parts.txt"
    lines = []
    for part in parts:
        escaped = part.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_file.write_text("\n".join(lines), encoding="utf-8")
    return ["-f", "concat", "-safe", "0", "-i", str(list_file)]


def _concat_videos(parts: List[Path], lengths: List[int], destination: Path, work_dir: Path,
                   fourcc_str: str, fps: float, dimensions: Tuple[int, int]) -> None:
    """Joins segment files with stream copy, falling back to a full re-encode if that fails.
    lengths: each part's length in slots, so the re-encode keeps every part's timing."""
    if len(parts) == 1:
        shutil.copyfile(parts[0], destination)
        return
    video_input = _video_input_arguments(parts, work_dir)
    succeeded, error = run_ffmpeg([*video_input, "-c", "copy", str(destination)])
    if succeeded:
        return

    print(f"Warning: joining replay segments failed ({error}). Re-encoding the clip instead, which is slower.")
    writer = open_video_writer(destination, fourcc_str, fps, dimensions, None, background=True)
    offset = 0
    try:
        for part, length in zip(parts, lengths):
            for slot, image in _decode_slots(part, fps):
                if slot < length:
                    writer.write(image, offset + slot)
            offset += length
    finally:
        writer.release(offset)


def _write_audio_range(layout: _SegmentLayout, track: str, destination: Path, start_sample: int, end_sample: int,
                       first_index: int, last_index: int, audio_format: Tuple[int, int]) -> None:
    """Copies exactly samples [start_sample, end_sample) of one track from the segment WAVs into one file."""
    bytes_per_frame = audio_format[1] * 2
    output = open_wav(destination, audio_format)
    try:
        for index in range(first_index, last_index + 1):
            segment_start = layout.segment_start_sample(index)
            segment_end = layout.segment_start_sample(index + 1)
            first = max(start_sample, segment_start)
            last = min(end_sample, segment_end)
            if first >= last:
                continue
            wanted = last - first
            data = b""
            try:
                with wave.open(str(layout.audio_path(index, track)), "rb") as segment:
                    available = segment.getnframes()
                    position = first - segment_start
                    if position < available:
                        segment.setpos(position)
                        data = segment.readframes(wanted)
            except (OSError, wave.Error, EOFError):
                data = b""  # No file: the track didn't exist yet (or had stopped) here
            missing = wanted - len(data) // bytes_per_frame
            if missing > 0:
                data += bytes(missing * bytes_per_frame)  # Silence where a segment came up short
            output.writeframesraw(data)
    finally:
        output.close()


def export_ring_clip(layout: _SegmentLayout, save_path: Path, start_frame: int, end_frame: int,
                     fourcc_str: str, fps: float, dimensions: Tuple[int, int],
                     audio_format: Optional[Tuple[int, int]], tracks: List[str],
                     encoder: Optional[Encoder]) -> None:
    """Builds a clip of exactly frames [start_frame, end_frame) and the matching audio samples."""
    segment_frames = layout.segment_frames
    first_index = start_frame // segment_frames
    last_index = (end_frame - 1) // segment_frames
    work_dir = Path(config.TEMP_DIR) / f"{save_path.stem}.export"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        parts: List[Path] = []
        lengths: List[int] = []
        for index in range(first_index, last_index + 1):
            segment_start = index * segment_frames
            first = max(start_frame, segment_start) - segment_start
            last = min(end_frame, segment_start + segment_frames) - segment_start
            source = layout.video_path(index)
            if first == 0 and last == segment_frames:
                parts.append(source)  # Whole segment: copied without re-encoding
            else:
                part = work_dir / f"part_{index:06d}.mp4"
                _reencode_frame_range(source, part, first, last, fourcc_str, fps, dimensions, encoder)
                parts.append(part)
            lengths.append(last - first)

        audio_tracks: List[AudioTrackFile] = []
        if audio_format is not None:
            for track in tracks:
                if not any(layout.audio_path(i, track).exists() for i in range(first_index, last_index + 1)):
                    continue  # The track has no audio anywhere in this clip
                audio_path = work_dir / f"audio.{track_slug(track)}.wav"
                _write_audio_range(
                    layout, track, audio_path, layout.sample_at_frame(start_frame), layout.sample_at_frame(end_frame),
                    first_index, last_index, audio_format,
                )
                audio_tracks.append((audio_path, track))
        if audio_tracks:
            # Fast path: join the segments and add the audio in a single ffmpeg pass,
            # reading and writing the video only once. No faststart, which would rewrite it again.
            video_input = _video_input_arguments(parts, work_dir)
            if mux_audio_video(None, audio_tracks, save_path, video_input=video_input, faststart=False):
                return
            print("Warning: one-pass clip export failed. Retrying in separate steps.")

        video_path = work_dir / "video.mp4"
        _concat_videos(parts, lengths, video_path, work_dir, fourcc_str, fps, dimensions)
        if audio_tracks:
            if mux_audio_video(video_path, audio_tracks, save_path, faststart=False):
                return
            print(f"Saved {save_path.name} without audio.")
        os.replace(video_path, save_path)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
