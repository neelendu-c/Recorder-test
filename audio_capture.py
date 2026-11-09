"""
Audio sources and routing (native WASAPI through ctypes COM).

Sources, each captured on its own thread as a separate *track*:
- "desktop": loopback of the default output device (the whole desktop mix), or, when one
  app is blocked, Windows' process loopback in EXCLUDE mode (everything except that app).
- "mic": the default Windows capture endpoint (microphone).
- "app:<exe>:<pid>": process loopback in INCLUDE mode, i.e. the audio of one application's
  process tree only (Windows 10 build 20348+ / Windows 11).

Every stream asks Windows for the same output format (AUTOCONVERTPCM), so tracks never
need resampling here. Each chunk is timestamped from WASAPI's QPC position of its first
sample, converted onto time.perf_counter(), the clock the video frames use. Consumers
place each track on the video timeline by those timestamps, so A/V sync is exact per
track, and the tracks are mixed (or kept as separate audio streams) when the file is muxed.

AudioRouter decides which streams should run from the config, starts/stops them on a
small manager thread, and restarts any that end (e.g. the default device changed).
"""
import ctypes
from ctypes import wintypes
import sys
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

import config
import win32_process

# Receives (track id, capture_time, int16 samples shaped (frames, channels)).
TrackSink = Callable[[str, float, np.ndarray], None]

RESCAN_SECONDS = 2.0        # How often running apps are re-checked for the routing rules
RETRY_SECONDS = 5.0         # Wait before restarting a stream that failed
BUFFER_100NS = 2_000_000    # 200 ms WASAPI buffer: room for Python scheduling hiccups
BATCH_SECONDS = 0.02        # Pause between buffer drains: ~30 wake-ups/s per stream instead of 100
ACTIVATE_TIMEOUT_SECONDS = 5.0
PROCESS_LOOPBACK_MIN_BUILD = 20348

# ----------------------------------------------------------------------------
# COM plumbing
# ----------------------------------------------------------------------------

ole32 = ctypes.WinDLL("ole32")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    _mmdevapi = ctypes.WinDLL("mmdevapi")
    ActivateAudioInterfaceAsync = _mmdevapi.ActivateAudioInterfaceAsync
except (OSError, AttributeError):
    ActivateAudioInterfaceAsync = None

HRESULT = ctypes.c_long


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> GUID:
    value = GUID()
    ctypes.memmove(ctypes.byref(value), uuid.UUID(text).bytes_le, 16)
    return value


CLSID_MMDeviceEnumerator = _guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IUnknown = _guid("{00000000-0000-0000-C000-000000000046}")
IID_IAgileObject = _guid("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
IID_IMMDeviceEnumerator = _guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioClient = _guid("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = _guid("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
IID_IAudioSessionManager2 = _guid("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
IID_IAudioSessionControl2 = _guid("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
IID_IActivateAudioInterfaceCompletionHandler = _guid("{41D949AB-9862-444A-80F6-C261334DA5EB}")

CLSCTX_ALL = 0x17
COINIT_MULTITHREADED = 0x0
E_NOINTERFACE = -2147467262  # 0x80004002
eRender, eCapture = 0, 1
eConsole = 0
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_S_BUFFER_EMPTY = 0x08890001
AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR = 0x4
AudioSessionStateActive = 1
WAVE_FORMAT_PCM = 1
VT_BLOB = 65
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
WAIT_TIMEOUT_MS = 100

ole32.CoInitializeEx.restype = HRESULT
ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
ole32.CoUninitialize.restype = None
ole32.CoCreateInstance.restype = HRESULT
ole32.CoCreateInstance.argtypes = (ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.QueryPerformanceCounter.argtypes = (ctypes.POINTER(ctypes.c_int64),)
kernel32.QueryPerformanceFrequency.argtypes = (ctypes.POINTER(ctypes.c_int64),)
if ActivateAudioInterfaceAsync is not None:
    ActivateAudioInterfaceAsync.restype = HRESULT
    ActivateAudioInterfaceAsync.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(GUID), ctypes.c_void_p,
                                            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", ctypes.c_int)]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int), ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS)]


class BLOB_PROPVARIANT(ctypes.Structure):
    """A PROPVARIANT holding a VT_BLOB (24 bytes on x64, like the real union)."""
    _fields_ = [("vt", ctypes.c_ushort), ("wReserved1", ctypes.c_ushort), ("wReserved2", ctypes.c_ushort),
                ("wReserved3", ctypes.c_ushort), ("cbSize", ctypes.c_ulong), ("pBlobData", ctypes.c_void_p)]


_prototypes: Dict[Tuple, object] = {}


def _method(obj: int, index: int, argtypes: Tuple):
    """Bound COM method `index` of the interface pointer obj (returns an HRESULT)."""
    key = (index, argtypes)
    prototype = _prototypes.get(key)
    if prototype is None:
        prototype = _prototypes[key] = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, *argtypes)
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = prototype(vtable[index])
    return lambda *args: function(obj, *args)


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(f"{what} failed (0x{hr & 0xFFFFFFFF:08X})")


def _release(obj: Optional[int]) -> None:
    if obj:
        try:
            _method(obj, 2, ())()
        except Exception:
            pass


def _query_interface(obj: int, iid: GUID) -> int:
    result = ctypes.c_void_p()
    _check(_method(obj, 0, (ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)))(
        ctypes.byref(iid), ctypes.byref(result)), "QueryInterface")
    return result.value


class _ComApartment:
    """CoInitializeEx(MTA) for the current thread, undone on exit if it succeeded."""

    def __enter__(self):
        self._hr = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        return self

    def __exit__(self, *_):
        if self._hr >= 0:
            ole32.CoUninitialize()


def _default_device(flow: int) -> int:
    enumerator = ctypes.c_void_p()
    _check(ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                  ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(enumerator)),
           "CoCreateInstance(MMDeviceEnumerator)")
    try:
        device = ctypes.c_void_p()
        _check(_method(enumerator.value, 4, (ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)))(
            flow, eConsole, ctypes.byref(device)), "GetDefaultAudioEndpoint")
        return device.value
    finally:
        _release(enumerator.value)


def _activate_on_device(device: int, iid: GUID) -> int:
    result = ctypes.c_void_p()
    _check(_method(device, 3, (ctypes.POINTER(GUID), wintypes.DWORD, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_void_p)))(
        ctypes.byref(iid), CLSCTX_ALL, None, ctypes.byref(result)), "IMMDevice::Activate")
    return result.value


# --- Completion handler for ActivateAudioInterfaceAsync ------------------------------------

_QI_PROTO = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
_REF_PROTO = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_COMPLETED_PROTO = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p)


class _HandlerVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", _QI_PROTO), ("AddRef", _REF_PROTO), ("Release", _REF_PROTO),
                ("ActivateCompleted", _COMPLETED_PROTO)]


class _HandlerObject(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_HandlerVtbl))]


# Handlers stay referenced for the life of the process: Windows may call Release on them
# after activation has completed, and a freed callback would crash.
_live_handlers: List["_CompletionHandler"] = []
_HANDLER_IIDS = {bytes(IID_IUnknown), bytes(IID_IAgileObject), bytes(IID_IActivateAudioInterfaceCompletionHandler)}


class _CompletionHandler:
    """A minimal free-threaded (agile) IActivateAudioInterfaceCompletionHandler."""

    def __init__(self):
        self.done = threading.Event()

        def query_interface(this, riid, ppv):
            if bytes(riid.contents) in _HANDLER_IIDS:
                ppv[0] = this
                return 0
            ppv[0] = None
            return E_NOINTERFACE

        def add_ref(_this):
            return 1

        def release(_this):
            return 1

        def activate_completed(_this, _operation):
            self.done.set()
            return 0

        self._vtbl = _HandlerVtbl(_QI_PROTO(query_interface), _REF_PROTO(add_ref), _REF_PROTO(release),
                                  _COMPLETED_PROTO(activate_completed))
        self._object = _HandlerObject(ctypes.pointer(self._vtbl))
        _live_handlers.append(self)

    @property
    def address(self) -> int:
        return ctypes.addressof(self._object)


def process_loopback_supported() -> bool:
    return ActivateAudioInterfaceAsync is not None and sys.getwindowsversion().build >= PROCESS_LOOPBACK_MIN_BUILD


def _activate_process_loopback(pid: int, exclude: bool) -> int:
    """An IAudioClient capturing one process tree (or everything except it)."""
    if not process_loopback_supported():
        raise OSError("per-application audio needs Windows 10 build 20348 or Windows 11")
    mode = PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE if exclude else PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
    params = AUDIOCLIENT_ACTIVATION_PARAMS(AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
                                           AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(pid, mode))
    prop = BLOB_PROPVARIANT(vt=VT_BLOB, cbSize=ctypes.sizeof(params), pBlobData=ctypes.addressof(params))
    handler = _CompletionHandler()
    operation = ctypes.c_void_p()
    _check(ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, ctypes.byref(IID_IAudioClient),
                                       ctypes.byref(prop), handler.address, ctypes.byref(operation)),
           "ActivateAudioInterfaceAsync")
    try:
        if not handler.done.wait(ACTIVATE_TIMEOUT_SECONDS):
            raise OSError("process loopback activation timed out")
        activate_hr = HRESULT()
        unknown = ctypes.c_void_p()
        _check(_method(operation.value, 3, (ctypes.POINTER(HRESULT), ctypes.POINTER(ctypes.c_void_p)))(
            ctypes.byref(activate_hr), ctypes.byref(unknown)), "GetActivateResult")
        _check(activate_hr.value, "process loopback activation")
        try:
            return _query_interface(unknown.value, IID_IAudioClient)
        finally:
            _release(unknown.value)
    finally:
        _release(operation.value)


def _qpc_to_perf_counter_offset() -> float:
    """Seconds to add to a QPC time (in seconds) to get time.perf_counter()."""
    frequency = ctypes.c_int64()
    counter = ctypes.c_int64()
    kernel32.QueryPerformanceFrequency(ctypes.byref(frequency))
    before = time.perf_counter()
    kernel32.QueryPerformanceCounter(ctypes.byref(counter))
    after = time.perf_counter()
    return (before + after) / 2 - counter.value / frequency.value


def output_format() -> Tuple[int, int]:
    return config.AUDIO_SAMPLE_RATE, config.AUDIO_CHANNELS


# ----------------------------------------------------------------------------
# Streams
# ----------------------------------------------------------------------------

class WasapiStream:
    """
    One shared-mode WASAPI capture stream on its own thread.
    kind: "loopback" (default output device), "mic" (default input device) or "process".
    """

    def __init__(self, track: str, kind: str, deliver: TrackSink, pid: Optional[int] = None, exclude: bool = False):
        self.track = track
        self.kind = kind
        self.pid = pid
        self.exclude = exclude
        self.alive = False
        self.error: Optional[str] = None
        self._deliver = deliver
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        """Starts capture. Returns False (with .error set) if the stream couldn't start."""
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"Audio[{self.track}]")
        self._thread.start()
        self._ready.wait(ACTIVATE_TIMEOUT_SECONDS + 2)
        return self.alive

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def _activate(self) -> int:
        if self.kind == "process":
            return _activate_process_loopback(self.pid, self.exclude)
        device = _default_device(eCapture if self.kind == "mic" else eRender)
        try:
            return _activate_on_device(device, IID_IAudioClient)
        finally:
            _release(device)

    def _run(self) -> None:
        client = capture = None
        event = None
        with _ComApartment():
            try:
                client = self._activate()
                rate, channels = output_format()
                wave_format = WAVEFORMATEX(WAVE_FORMAT_PCM, channels, rate, rate * channels * 2, channels * 2, 16, 0)
                flags = AUDCLNT_STREAMFLAGS_EVENTCALLBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
                if self.kind != "process":
                    flags |= AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
                if self.kind in ("loopback", "process"):
                    flags |= AUDCLNT_STREAMFLAGS_LOOPBACK
                _check(_method(client, 3, (ctypes.c_int, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong,
                                           ctypes.c_void_p, ctypes.c_void_p))(
                    AUDCLNT_SHAREMODE_SHARED, flags, BUFFER_100NS, 0, ctypes.byref(wave_format), None),
                    "IAudioClient::Initialize")
                event = kernel32.CreateEventW(None, False, False, None)
                if not event:
                    raise OSError("CreateEvent failed")
                _check(_method(client, 13, (wintypes.HANDLE,))(event), "SetEventHandle")
                capture_ptr = ctypes.c_void_p()
                _check(_method(client, 14, (ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)))(
                    ctypes.byref(IID_IAudioCaptureClient), ctypes.byref(capture_ptr)), "GetService")
                capture = capture_ptr.value
                _check(_method(client, 10, ())(), "IAudioClient::Start")
                self.alive = True
                self._ready.set()
                self._loop(capture, event, rate, channels)
            except Exception as e:
                self.error = str(e)
                if self.alive:
                    print(f"Warning: audio track '{self.track}' stopped: {e}")
            finally:
                self.alive = False
                self._ready.set()
                if client:
                    try:
                        _method(client, 11, ())()  # Stop
                    except Exception:
                        pass
                _release(capture)
                _release(client)
                if event:
                    kernel32.CloseHandle(event)

    def _loop(self, capture: int, event, rate: int, channels: int) -> None:
        block = channels * 2
        clock_offset = _qpc_to_perf_counter_offset()
        sync_offset = config.AUDIO_SYNC_OFFSET_MS / 1000.0
        get_next_packet = _method(capture, 5, (ctypes.POINTER(ctypes.c_uint32),))
        get_buffer = _method(capture, 3, (ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32),
                                          ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_uint64),
                                          ctypes.POINTER(ctypes.c_uint64)))
        release_buffer = _method(capture, 4, (ctypes.c_uint32,))
        packet = ctypes.c_uint32()
        data = ctypes.c_void_p()
        frames = ctypes.c_uint32()
        flags = wintypes.DWORD()
        device_position = ctypes.c_uint64()
        qpc_position = ctypes.c_uint64()
        packet_ref = ctypes.byref(packet)
        buffer_refs = (ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags),
                       ctypes.byref(device_position), ctypes.byref(qpc_position))
        string_at = ctypes.string_at
        perf_counter = time.perf_counter
        stop = self._stop

        # Packets that arrived since the last wake-up and follow on from each other are merged
        # into one chunk: (capture_time, next expected device position, frame count, byte parts).
        run_time = 0.0
        run_next = -1
        run_frames = 0
        run_parts: List[bytes] = []

        def deliver_run():
            if run_frames:
                samples = np.frombuffer(b"".join(run_parts), dtype=np.int16).reshape(run_frames, channels)
                self._deliver(self.track, run_time + sync_offset, samples)

        while not stop.is_set():
            kernel32.WaitForSingleObject(event, WAIT_TIMEOUT_MS)
            while True:
                _check(get_next_packet(packet_ref), "GetNextPacketSize")
                if packet.value == 0:
                    break
                hr = get_buffer(*buffer_refs)
                if hr == AUDCLNT_S_BUFFER_EMPTY:
                    break
                _check(hr, "GetBuffer")
                count = frames.value
                packet_flags = flags.value
                if (packet_flags & AUDCLNT_BUFFERFLAGS_SILENT) or not data.value:
                    chunk = bytes(count * block)
                else:
                    chunk = string_at(data.value, count * block)
                release_buffer(count)
                if count == 0:
                    continue

                position = device_position.value
                if position == run_next and not (packet_flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY):
                    # Continues the current run: its timing follows from the run's first sample.
                    run_parts.append(chunk)
                    run_frames += count
                    run_next = position + count
                    continue

                deliver_run()
                now = perf_counter()
                # QPC position of the first sample, in 100 ns units: the same clock as perf_counter.
                capture_time = qpc_position.value * 1e-7 + clock_offset
                if (not qpc_position.value or (packet_flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR)
                        or abs(capture_time - now) > 1.0):
                    capture_time = now - count / rate
                run_time, run_next, run_frames, run_parts = capture_time, position + count, count, [chunk]

            deliver_run()
            run_frames, run_parts, run_next = 0, [], -1
            # Waking for every 10 ms packet costs a GIL handoff each time; the 200 ms WASAPI
            # buffer holds plenty, so packets are collected in batches instead.
            stop.wait(BATCH_SECONDS)


class FormatConverter:
    """Converts int16 chunks to the output format (used only for the PyAudio fallback)."""

    def __init__(self, source_rate: int, source_channels: int, rate: int, channels: int):
        self.source_rate, self.source_channels = source_rate, source_channels
        self.rate, self.channels = rate, channels
        self._in_total = 0
        self._out_total = 0

    def convert(self, samples: np.ndarray) -> np.ndarray:
        if self.source_channels != self.channels:
            if self.source_channels == 1:
                samples = np.repeat(samples, self.channels, axis=1)
            elif self.channels == 1:
                samples = samples.mean(axis=1, keepdims=True).astype(np.int16)
            elif self.source_channels > self.channels:
                samples = samples[:, :self.channels]  # Front left/right of a surround mix
            else:
                samples = np.concatenate([samples] + [samples[:, -1:]] * (self.channels - self.source_channels), axis=1)
        if self.source_rate == self.rate or not len(samples):
            return np.ascontiguousarray(samples)
        # Running totals keep the output length exact over time (no rounding drift per chunk).
        self._in_total += len(samples)
        target_total = round(self._in_total * self.rate / self.source_rate)
        count = target_total - self._out_total
        self._out_total = target_total
        if count <= 0:
            return samples[:0]
        positions = np.linspace(0, len(samples) - 1, count)
        indices = np.arange(len(samples))
        output = np.empty((count, samples.shape[1]), dtype=np.int16)
        for channel in range(samples.shape[1]):
            output[:, channel] = np.interp(positions, indices, samples[:, channel])
        return output


class PyAudioDesktopStream:
    """Desktop audio through PyAudioWPatch, used if native WASAPI loopback can't start."""

    kind = "loopback"

    def __init__(self, track: str, deliver: TrackSink):
        from video_recorder import LoopbackAudioCapture
        self.track = track
        self.alive = False
        self.error: Optional[str] = None
        self._deliver = deliver
        self._capture = LoopbackAudioCapture()
        self._converter: Optional[FormatConverter] = None

    def start(self) -> bool:
        if not self._capture.start():
            self.error = "PyAudioWPatch loopback unavailable"
            return False
        source_rate, source_channels = self._capture.audio_format
        self._converter = FormatConverter(source_rate, source_channels, *output_format())
        self._capture.add_sink(self._on_chunk)
        self.alive = True
        return True

    def _on_chunk(self, capture_time: float, samples: np.ndarray) -> None:
        self._deliver(self.track, capture_time, self._converter.convert(samples))

    def stop(self) -> None:
        self.alive = False
        self._capture.remove_sink(self._on_chunk)
        self._capture.stop()


# ----------------------------------------------------------------------------
# Audio sessions (which processes are playing sound)
# ----------------------------------------------------------------------------

def audio_session_pids() -> Dict[int, bool]:
    """pid -> whether its session is playing now, for sessions on the default output device.
    Needs a COM-initialized (MTA) thread."""
    device = _default_device(eRender)
    manager = sessions = None
    found: Dict[int, bool] = {}
    try:
        manager = _activate_on_device(device, IID_IAudioSessionManager2)
        sessions_ptr = ctypes.c_void_p()
        _check(_method(manager, 5, (ctypes.POINTER(ctypes.c_void_p),))(ctypes.byref(sessions_ptr)),
               "GetSessionEnumerator")
        sessions = sessions_ptr.value
        count = ctypes.c_int()
        _check(_method(sessions, 3, (ctypes.POINTER(ctypes.c_int),))(ctypes.byref(count)), "GetCount")
        for index in range(count.value):
            control = ctypes.c_void_p()
            if _method(sessions, 4, (ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)))(index, ctypes.byref(control)) < 0:
                continue
            control2 = None
            try:
                control2 = _query_interface(control.value, IID_IAudioSessionControl2)
                state = ctypes.c_int()
                _method(control.value, 3, (ctypes.POINTER(ctypes.c_int),))(ctypes.byref(state))
                pid = wintypes.DWORD()
                _method(control2, 14, (ctypes.POINTER(wintypes.DWORD),))(ctypes.byref(pid))
                if pid.value:
                    found[pid.value] = found.get(pid.value, False) or state.value == AudioSessionStateActive
            except OSError:
                pass
            finally:
                _release(control2)
                _release(control.value)
    finally:
        _release(sessions)
        _release(manager)
        _release(device)
    return found


# ----------------------------------------------------------------------------
# Router
# ----------------------------------------------------------------------------

StreamSpec = Tuple[str, Optional[int], bool]  # (kind, pid, exclude)


class AudioRouter:
    """
    Runs the audio streams the config asks for and passes their chunks to sinks.

    Same interface the recorder used for the single loopback capture (audio_format,
    add_sink, remove_sink, start, stop), but sinks receive (track, capture_time, samples).
    Call apply() after changing any AUDIO_* setting.
    """

    def __init__(self):
        self._sinks: Tuple[TrackSink, ...] = ()
        self._sinks_lock = threading.Lock()
        self._streams: Dict[str, object] = {}
        self._specs: Dict[str, StreamSpec] = {}
        self._retry_at: Dict[str, float] = {}
        self._wake = threading.Event()
        self._stopping = False
        self._thread: Optional[threading.Thread] = None
        self._catalog_requested = False
        self._started = False

        # Read by the UI
        self.track_status: Dict[str, str] = {}   # track -> "live" or an error message
        self.catalog: List[Dict] = []            # running apps, for Settings > Audio
        self.catalog_version = 0
        self.status_version = 0
        self.process_loopback_supported = process_loopback_supported()

    @property
    def audio_format(self) -> Optional[Tuple[int, int]]:
        """The format of every track while audio is enabled, otherwise None."""
        return output_format() if (self._started and config.AUDIO_ENABLED) else None

    def add_sink(self, sink: TrackSink) -> None:
        # Copy-on-write: the audio threads read self._sinks without taking the lock.
        with self._sinks_lock:
            self._sinks = (*self._sinks, sink)

    def remove_sink(self, sink: TrackSink) -> None:
        with self._sinks_lock:
            sinks = list(self._sinks)
            if sink in sinks:
                sinks.remove(sink)
                self._sinks = tuple(sinks)

    def start(self) -> bool:
        if not config.AUDIO_ENABLED or self._started:
            return self._started
        self._started = True
        self._thread = threading.Thread(target=self._manager, daemon=True, name="AudioRouter")
        self._thread.start()
        return True

    def apply(self) -> None:
        """Re-evaluates the routing now (after a settings change)."""
        self._wake.set()

    def request_catalog(self) -> None:
        """Asks the manager thread to list running apps; catalog_version changes when done."""
        self._catalog_requested = True
        self._wake.set()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        for stream in list(self._streams.values()):
            stream.stop()
        self._streams.clear()

    def _deliver(self, track: str, capture_time: float, samples: np.ndarray) -> None:
        for sink in self._sinks:  # An immutable tuple, replaced whole on add/remove
            try:
                sink(track, capture_time, samples)
            except Exception as e:
                print(f"Warning: audio sink failed: {e}")

    # --- Manager thread ---------------------------------------------------------------

    def _manager(self) -> None:
        with _ComApartment():
            while not self._stopping:
                try:
                    if self._catalog_requested:
                        self._catalog_requested = False
                        self.catalog = self._build_catalog(win32_process.ProcessSnapshot())
                        self.catalog_version += 1
                    self._reconcile()
                except Exception as e:
                    print(f"Warning: audio routing error: {e}")
                self._wake.wait(RESCAN_SECONDS)
                self._wake.clear()
            for stream in list(self._streams.values()):
                stream.stop()
            self._streams.clear()

    def _desired_streams(self) -> Dict[str, StreamSpec]:
        desired: Dict[str, StreamSpec] = {}
        if config.AUDIO_MIC_ENABLED:
            desired["mic"] = ("mic", None, False)

        mode = config.AUDIO_DESKTOP_MODE
        if mode == "all" and not config.AUDIO_BLOCKED_APPS:
            desired["desktop"] = ("loopback", None, False)  # No process scan needed
        elif mode == "all":
            snapshot = win32_process.ProcessSnapshot()
            roots = snapshot.roots_by_exe()
            blocked = set(config.AUDIO_BLOCKED_APPS)
            blocked_roots = [pid for exe in blocked for pid in roots.get(exe, [])]
            if not blocked_roots or not self.process_loopback_supported:
                desired["desktop"] = ("loopback", None, False)
            elif len(blocked_roots) == 1:
                # Everything except the one blocked app, in a single stream.
                desired["desktop"] = ("process", blocked_roots[0], True)
            else:
                # Windows can exclude only one process tree per stream, so with several blocked
                # apps every other app that has an audio session is recorded on its own.
                for pid in audio_session_pids():
                    exe = snapshot.exe_of.get(pid)
                    if not exe or exe in blocked or pid == snapshot.own_pid:
                        continue
                    root = snapshot.root_of(pid)
                    desired[f"app:{exe}:{root}"] = ("process", root, False)
        elif mode == "selected" and self.process_loopback_supported and config.AUDIO_INCLUDED_APPS:
            roots = win32_process.ProcessSnapshot().roots_by_exe()
            for exe in config.AUDIO_INCLUDED_APPS:
                for pid in roots.get(exe, []):
                    desired[f"app:{exe}:{pid}"] = ("process", pid, False)
        return desired

    def _reconcile(self) -> None:
        desired = self._desired_streams() if config.AUDIO_ENABLED else {}
        previous_status = dict(self.track_status)

        for track in list(self._streams):
            stream = self._streams[track]
            if desired.get(track) != self._specs.get(track) or not stream.alive:
                stream.stop()
                del self._streams[track]
                self._specs.pop(track, None)
                if track not in desired:
                    self.track_status.pop(track, None)
                    self._retry_at.pop(track, None)

        now = time.perf_counter()
        for track, spec in desired.items():
            if track in self._streams or now < self._retry_at.get(track, 0.0):
                continue
            stream = self._open_stream(track, spec)
            if stream is not None:
                self._streams[track] = stream
                self._specs[track] = spec
                self.track_status[track] = "live"
                self._retry_at.pop(track, None)
            else:
                self._retry_at[track] = now + RETRY_SECONDS

        for track in list(self.track_status):
            if track not in desired:
                del self.track_status[track]
        # The UI redraws its cached layout on a version change, so only real changes count
        # (a source retrying with the same error every few seconds doesn't).
        if self.track_status != previous_status:
            self.status_version += 1

    def _open_stream(self, track: str, spec: StreamSpec):
        kind, pid, exclude = spec
        stream = WasapiStream(track, kind, self._deliver, pid, exclude)
        if stream.start():
            return stream
        error = stream.error or "could not start"
        if kind == "loopback":
            fallback = PyAudioDesktopStream(track, self._deliver)
            if fallback.start():
                print(f"Native desktop audio failed ({error}); using PyAudioWPatch instead.")
                return fallback
        print(f"Warning: could not capture audio track '{track}': {error}")
        self.track_status[track] = error
        return None

    def _build_catalog(self, snapshot: win32_process.ProcessSnapshot) -> List[Dict]:
        """Apps the user can route: ones with an audio session or a window, plus any with a saved rule."""
        import win32_api  # Window enumeration lives with the other window helpers

        apps: Dict[str, Dict] = {}

        def entry(exe: str) -> Dict:
            return apps.setdefault(exe, {"exe": exe, "title": "", "playing": False, "has_audio": False,
                                         "running": True})

        try:
            sessions = audio_session_pids()
        except OSError:
            sessions = {}
        for pid, playing in sessions.items():
            exe = snapshot.exe_of.get(pid)
            if exe and pid != snapshot.own_pid and exe not in win32_process.IGNORED_EXES:
                app = entry(exe)
                app["has_audio"] = True
                app["playing"] = app["playing"] or playing
        for hwnd, title in win32_api.list_windows():
            exe = snapshot.exe_of.get(win32_process.window_process_id(hwnd))
            if exe and exe not in win32_process.IGNORED_EXES:
                app = entry(exe)
                if not app["title"]:
                    app["title"] = title
        for exe in list(config.AUDIO_INCLUDED_APPS) + list(config.AUDIO_BLOCKED_APPS):
            if exe not in apps:
                apps[exe] = {"exe": exe, "title": "", "playing": False, "has_audio": False, "running": False}

        return sorted(apps.values(), key=lambda a: (not a["playing"], not a["has_audio"], not a["running"], a["exe"]))
