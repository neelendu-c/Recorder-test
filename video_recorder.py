"""
System audio capture (WASAPI loopback) for the screen recorder.
Also remains the backwards-compatible entry point for the application in main.py.
"""
import threading
import time
from typing import Callable, List, Optional, Tuple

import numpy as np

import config

# Receives (capture_time, samples): capture_time is the time.perf_counter() value of the
# chunk's first sample, and samples is an int16 array shaped (frames, channels).
AudioSink = Callable[[float, np.ndarray], None]


class LoopbackAudioCapture:
    """
    Captures system audio and passes timestamped chunks to registered sinks.

    PyAudioWPatch runs the stream callback on its own thread. Callback mode also means
    stop() cannot hang while the system is silent and loopback delivers no data.
    If the library or device is unavailable, the app carries on recording video only.
    """

    def __init__(self):
        self.sample_rate: Optional[int] = None
        self.channels: Optional[int] = None
        self._pyaudio = None
        self._stream = None
        self._sinks: List[AudioSink] = []
        self._sinks_lock = threading.Lock()
        self._offset_seconds = config.AUDIO_SYNC_OFFSET_MS / 1000.0

    @property
    def audio_format(self) -> Optional[Tuple[int, int]]:
        """Returns (sample_rate, channels) while capturing, otherwise None."""
        if self._stream is None:
            return None
        return self.sample_rate, self.channels

    def add_sink(self, sink: AudioSink) -> None:
        """Registers a function to receive each captured chunk. It must return quickly."""
        with self._sinks_lock:
            self._sinks.append(sink)

    def remove_sink(self, sink: AudioSink) -> None:
        """Unregisters a sink. Does nothing if it is not registered."""
        with self._sinks_lock:
            if sink in self._sinks:
                self._sinks.remove(sink)

    def start(self) -> bool:
        """Opens the loopback stream. Returns False if audio is disabled or unavailable."""
        if not config.AUDIO_ENABLED or self._stream is not None:
            return self._stream is not None

        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            print("Warning: PyAudioWPatch is not installed. Recording video without audio.")
            return False

        try:
            self._pyaudio = pyaudio.PyAudio()
            device = self._find_loopback_device(pyaudio)
            self.sample_rate = int(device["defaultSampleRate"])
            self.channels = int(device["maxInputChannels"])
            continue_flag = pyaudio.paContinue

            def _callback(in_data, frame_count, _time_info, _status):
                self._on_audio(in_data, frame_count)
                return (None, continue_flag)

            self._stream = self._pyaudio.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                input_device_index=device["index"],
                frames_per_buffer=config.AUDIO_CHUNK_FRAMES,
                stream_callback=_callback,
            )
        except Exception as e:
            print(f"Warning: could not start system audio capture ({e}). Recording video without audio.")
            self.stop()
            return False

        print(f"Capturing system audio from: {device['name']} ({self.sample_rate} Hz, {self.channels} ch)")
        return True

    def stop(self) -> None:
        """Stops capture and releases the audio device. Safe to call more than once."""
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception as e:
                print(f"Warning: error while stopping audio capture: {e}")
            self._stream = None

        if self._pyaudio is not None:
            self._pyaudio.terminate()
            self._pyaudio = None

    def _find_loopback_device(self, pyaudio) -> dict:
        """Finds the configured loopback device, or the default output's loopback device."""
        if config.AUDIO_DEVICE:
            wanted = config.AUDIO_DEVICE.lower()
        else:
            wasapi_info = self._pyaudio.get_host_api_info_by_type(pyaudio.paWASAPI)
            output_device = self._pyaudio.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
            if output_device.get("isLoopbackDevice"):
                return output_device
            wanted = output_device["name"].lower()

        for device in self._pyaudio.get_loopback_device_info_generator():
            if wanted in device["name"].lower():
                return device
        raise RuntimeError(f"no loopback device matches '{wanted}'")

    def _on_audio(self, in_data: bytes, frame_count: int) -> None:
        """Timestamps a chunk and passes it to every sink."""
        capture_time = time.perf_counter() - frame_count / self.sample_rate + self._offset_seconds
        samples = np.frombuffer(in_data, dtype=np.int16).reshape(-1, self.channels)

        with self._sinks_lock:
            sinks = list(self._sinks)
        for sink in sinks:
            try:
                sink(capture_time, samples)
            except Exception as e:
                # An exception escaping into the stream callback would end the stream.
                print(f"Warning: audio sink failed: {e}")


if __name__ == "__main__":
    # Imported here because main.py imports this module.
    from main import main

    main()
