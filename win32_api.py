"""
Minimal ctypes bindings for the Win32 calls used by the capture sources and the live overlay.
Only declarations and small helpers live here; nothing runs at import time beyond loading DLLs.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from typing import Dict, List, Optional, Tuple

import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

LRESULT = ctypes.c_ssize_t
HANDLE = wintypes.HANDLE

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)

# Window styles
GWL_EXSTYLE = -20
WS_POPUP = 0x80000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4
SW_MAXIMIZE = 3

# Window thumbnails
PW_RENDERFULLCONTENT = 0x00000002  # Includes DirectX/composited content (Windows 8.1+)
WM_GETICON = 0x007F
ICON_BIG = 1
ICON_SMALL2 = 2
GCLP_HICON = -14
GCLP_HICONSM = -34
SMTO_ABORTIFHUNG = 0x0002
DI_NORMAL = 0x0003

# Layered window drawing
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0
DIB_RGB_COLORS = 0

# Capture exclusion (Windows 10 2004+): shown on the monitor, left out of every capture API.
WDA_EXCLUDEFROMCAPTURE = 0x00000011

DWMWA_CLOAKED = 14
GA_ROOT = 2
MONITOR_DEFAULTTONEAREST = 0x00000002
MONITORINFOF_PRIMARY = 0x00000001
PM_REMOVE = 0x0001
QS_ALLINPUT = 0x04FF
ERROR_CLASS_ALREADY_EXISTS = 1410
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", HANDLE),
        ("hIcon", HANDLE),
        ("hCursor", HANDLE),
        ("hbrBackground", HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", HANDLE),
    ]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _declare(dll, name, restype, *argtypes):
    func = getattr(dll, name)
    func.restype = restype
    func.argtypes = argtypes
    return func


# user32
EnumWindows = _declare(user32, "EnumWindows", wintypes.BOOL, WNDENUMPROC, wintypes.LPARAM)
IsWindow = _declare(user32, "IsWindow", wintypes.BOOL, wintypes.HWND)
IsWindowVisible = _declare(user32, "IsWindowVisible", wintypes.BOOL, wintypes.HWND)
GetWindowTextLengthW = _declare(user32, "GetWindowTextLengthW", ctypes.c_int, wintypes.HWND)
GetWindowTextW = _declare(user32, "GetWindowTextW", ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
GetWindowLongPtrW = _declare(user32, "GetWindowLongPtrW", ctypes.c_ssize_t, wintypes.HWND, ctypes.c_int)
GetWindowThreadProcessId = _declare(
    user32, "GetWindowThreadProcessId", wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
)
GetForegroundWindow = _declare(user32, "GetForegroundWindow", wintypes.HWND)
GetAncestor = _declare(user32, "GetAncestor", wintypes.HWND, wintypes.HWND, wintypes.UINT)
SetWindowDisplayAffinity = _declare(user32, "SetWindowDisplayAffinity", wintypes.BOOL, wintypes.HWND, wintypes.DWORD)
GetWindowDisplayAffinity = _declare(
    user32, "GetWindowDisplayAffinity", wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
)
MonitorFromWindow = _declare(user32, "MonitorFromWindow", wintypes.HMONITOR, wintypes.HWND, wintypes.DWORD)
GetMonitorInfoW = _declare(user32, "GetMonitorInfoW", wintypes.BOOL, wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW))
EnumDisplayMonitors = _declare(
    user32, "EnumDisplayMonitors", wintypes.BOOL, wintypes.HDC, ctypes.c_void_p, MONITORENUMPROC, wintypes.LPARAM
)
RegisterClassExW = _declare(user32, "RegisterClassExW", wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
UnregisterClassW = _declare(user32, "UnregisterClassW", wintypes.BOOL, wintypes.LPCWSTR, HANDLE)
CreateWindowExW = _declare(
    user32, "CreateWindowExW", wintypes.HWND,
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, HANDLE, HANDLE, ctypes.c_void_p,
)
DefWindowProcW = _declare(user32, "DefWindowProcW", LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
DestroyWindow = _declare(user32, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
ShowWindow = _declare(user32, "ShowWindow", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
FindWindowW = _declare(user32, "FindWindowW", wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
ShowWindowAsync = _declare(user32, "ShowWindowAsync", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
IsIconic = _declare(user32, "IsIconic", wintypes.BOOL, wintypes.HWND)
GetWindowRect = _declare(user32, "GetWindowRect", wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
PrintWindow = _declare(user32, "PrintWindow", wintypes.BOOL, wintypes.HWND, wintypes.HDC, wintypes.UINT)
SendMessageTimeoutW = _declare(
    user32, "SendMessageTimeoutW", LRESULT,
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
    ctypes.POINTER(ctypes.c_size_t),
)
GetClassLongPtrW = _declare(user32, "GetClassLongPtrW", ctypes.c_size_t, wintypes.HWND, ctypes.c_int)
DrawIconEx = _declare(
    user32, "DrawIconEx", wintypes.BOOL,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, HANDLE, ctypes.c_int, ctypes.c_int, wintypes.UINT, HANDLE, wintypes.UINT,
)
UpdateLayeredWindow = _declare(
    user32, "UpdateLayeredWindow", wintypes.BOOL,
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
    wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.DWORD, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
)
GetDC = _declare(user32, "GetDC", wintypes.HDC, wintypes.HWND)
ReleaseDC = _declare(user32, "ReleaseDC", ctypes.c_int, wintypes.HWND, wintypes.HDC)
PeekMessageW = _declare(
    user32, "PeekMessageW", wintypes.BOOL,
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT,
)
TranslateMessage = _declare(user32, "TranslateMessage", wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
DispatchMessageW = _declare(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wintypes.MSG))
MsgWaitForMultipleObjects = _declare(
    user32, "MsgWaitForMultipleObjects", wintypes.DWORD,
    wintypes.DWORD, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, wintypes.DWORD,
)
try:
    SetThreadDpiAwarenessContext = _declare(user32, "SetThreadDpiAwarenessContext", ctypes.c_void_p, ctypes.c_void_p)
except AttributeError:  # Older than Windows 10 1607
    SetThreadDpiAwarenessContext = None

# gdi32
CreateCompatibleDC = _declare(gdi32, "CreateCompatibleDC", wintypes.HDC, wintypes.HDC)
CreateDIBSection = _declare(
    gdi32, "CreateDIBSection", HANDLE,
    wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), HANDLE, wintypes.DWORD,
)
SelectObject = _declare(gdi32, "SelectObject", HANDLE, wintypes.HDC, HANDLE)
DeleteObject = _declare(gdi32, "DeleteObject", wintypes.BOOL, HANDLE)
DeleteDC = _declare(gdi32, "DeleteDC", wintypes.BOOL, wintypes.HDC)

# kernel32
GetModuleHandleW = _declare(kernel32, "GetModuleHandleW", HANDLE, wintypes.LPCWSTR)
GlobalMemoryStatusEx = _declare(kernel32, "GlobalMemoryStatusEx", wintypes.BOOL, ctypes.POINTER(MEMORYSTATUSEX))

# dwmapi
DwmGetWindowAttribute = _declare(
    dwmapi, "DwmGetWindowAttribute", ctypes.c_long, wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD
)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def maximize_window(title: str) -> bool:
    """Maximizes the top-level window with this exact title. Returns False if it isn't found."""
    hwnd = FindWindowW(None, title)
    if not hwnd:
        return False
    ShowWindow(hwnd, SW_MAXIMIZE)
    return True


def get_window_title(hwnd: int) -> str:
    length = GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def is_window(hwnd: Optional[int]) -> bool:
    return bool(hwnd) and bool(IsWindow(hwnd))


def is_own_process_window(hwnd: int) -> bool:
    pid = wintypes.DWORD(0)
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == os.getpid()


def _is_cloaked(hwnd: int) -> bool:
    cloaked = wintypes.DWORD(0)
    result = DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return result == 0 and cloaked.value != 0


def is_capturable_window(hwnd: int) -> bool:
    """True for visible, titled, top-level application windows that don't belong to this app."""
    if not hwnd or not IsWindowVisible(hwnd) or GetAncestor(hwnd, GA_ROOT) != hwnd:
        return False
    ex_style = GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if (ex_style & WS_EX_TOOLWINDOW) and not (ex_style & WS_EX_APPWINDOW):
        return False
    if _is_cloaked(hwnd) or is_own_process_window(hwnd):
        return False
    return bool(get_window_title(hwnd).strip())


def list_windows() -> List[Tuple[int, str]]:
    """Returns (hwnd, title) for every capturable top-level window, in z-order."""
    found: List[Tuple[int, str]] = []

    def _callback(hwnd, _lparam):
        if is_capturable_window(hwnd):
            found.append((hwnd, get_window_title(hwnd)))
        return True

    EnumWindows(WNDENUMPROC(_callback), 0)
    return found


def foreground_window() -> Optional[int]:
    """Returns the active top-level window if it can be captured, otherwise None."""
    hwnd = GetForegroundWindow()
    if not hwnd:
        return None
    root = GetAncestor(hwnd, GA_ROOT) or hwnd
    return root if is_capturable_window(root) else None


def exclude_from_capture(hwnd: int) -> bool:
    """Hides a window from every capture API and confirms Windows applied it."""
    if not SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
        return False
    affinity = wintypes.DWORD(0)
    if not GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)):
        return False
    return affinity.value == WDA_EXCLUDEFROMCAPTURE


def _monitor_info(hmonitor) -> Optional[Dict]:
    info = MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(MONITORINFOEXW)
    if not GetMonitorInfoW(hmonitor, ctypes.byref(info)):
        return None
    rect = info.rcMonitor
    return {
        "device": info.szDevice,
        "rect": (rect.left, rect.top, rect.right, rect.bottom),
        "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
    }


def list_monitors() -> List[Dict]:
    """Returns each monitor's device name (e.g. '\\\\.\\DISPLAY1'), rectangle, and primary flag."""
    monitors: List[Dict] = []

    def _callback(hmonitor, _hdc, _rect, _lparam):
        info = _monitor_info(hmonitor)
        if info is not None:
            monitors.append(info)
        return True

    EnumDisplayMonitors(None, None, MONITORENUMPROC(_callback), 0)
    return monitors


def monitor_for_window(hwnd: int) -> Optional[Dict]:
    return _monitor_info(MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST))


@contextmanager
def per_monitor_dpi():
    """
    Runs the enclosed calls with per-monitor DPI awareness on this thread, so window and
    monitor rectangles are in physical pixels (the same space the overlay thread uses).
    """
    previous = None
    if SetThreadDpiAwarenessContext is not None:
        previous = SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    try:
        yield
    finally:
        if previous:
            SetThreadDpiAwarenessContext(previous)


# --- Minimized windows ---------------------------------------------------------------

def is_minimized(hwnd: int) -> bool:
    return bool(hwnd) and bool(IsIconic(hwnd))


def restore_without_activating(hwnd: int) -> None:
    """
    Un-minimizes a window without giving it focus. Asynchronous, so a hung application
    can't block the caller. Windows doesn't render minimized windows, so this is the only
    way their capture feed can update.
    """
    ShowWindowAsync(hwnd, SW_SHOWNOACTIVATE)


# --- Window thumbnails ---------------------------------------------------------------

class _DibCanvas:
    """A memory DC with a top-down 32-bit DIB section, readable as a numpy BGRA array."""

    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        self.dc = CreateCompatibleDC(None)
        if not self.dc:
            raise OSError("CreateCompatibleDC failed")
        info = BITMAPINFO()
        header = info.bmiHeader
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB
        self._bits = ctypes.c_void_p()
        self._bitmap = CreateDIBSection(self.dc, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(self._bits), None, 0)
        if not self._bitmap:
            DeleteDC(self.dc)
            raise OSError("CreateDIBSection failed")
        self._old = SelectObject(self.dc, self._bitmap)

    def array(self) -> np.ndarray:
        """A view of the DIB's pixels (valid until close)."""
        buffer = (ctypes.c_ubyte * (self.width * self.height * 4)).from_address(self._bits.value)
        return np.ndarray((self.height, self.width, 4), dtype=np.uint8, buffer=buffer)

    def close(self) -> None:
        SelectObject(self.dc, self._old)
        DeleteObject(self._bitmap)
        DeleteDC(self.dc)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def capture_window_image(hwnd: int) -> Optional[np.ndarray]:
    """
    One still BGR image of a window via PrintWindow, or None (minimized, zero-sized, or
    the app refused). The call is answered by the target app's own thread, so run it on a
    background thread: a hung application would block the caller.
    """
    rect = wintypes.RECT()
    if is_minimized(hwnd) or not GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0 or width * height > 8192 * 8192:
        return None
    try:
        with _DibCanvas(width, height) as canvas:
            if not PrintWindow(hwnd, canvas.dc, PW_RENDERFULLCONTENT):
                return None
            return canvas.array()[:, :, :3].copy()
    except OSError:
        return None


def window_icon_image(hwnd: int, size: int, background: Tuple[int, int, int]) -> Optional[np.ndarray]:
    """The window's application icon drawn on a solid background, as BGR, or None."""
    result = ctypes.c_size_t(0)
    hicon = 0
    for icon_type in (ICON_BIG, ICON_SMALL2):
        if SendMessageTimeoutW(hwnd, WM_GETICON, icon_type, 0, SMTO_ABORTIFHUNG, 100, ctypes.byref(result)):
            hicon = result.value
        if hicon:
            break
    if not hicon:
        hicon = GetClassLongPtrW(hwnd, GCLP_HICON) or GetClassLongPtrW(hwnd, GCLP_HICONSM)
    if not hicon:
        return None
    try:
        with _DibCanvas(size, size) as canvas:
            pixels = canvas.array()
            pixels[:, :, :3] = background
            pixels[:, :, 3] = 255
            if not DrawIconEx(canvas.dc, 0, 0, hicon, size, size, 0, None, DI_NORMAL):
                return None
            return pixels[:, :, :3].copy()
    except OSError:
        return None


def total_physical_memory_bytes() -> int:
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0
    return int(status.ullTotalPhys)


# --- Thread priority ----------------------------------------------------------------

THREAD_PRIORITY_BELOW_NORMAL = -1
THREAD_PRIORITY_ABOVE_NORMAL = 1
THREAD_PRIORITY_HIGHEST = 2
THREAD_SET_LIMITED_INFORMATION = 0x0400

_SetThreadPriority = kernel32.SetThreadPriority
_SetThreadPriority.argtypes = (wintypes.HANDLE, ctypes.c_int)
_SetThreadPriority.restype = wintypes.BOOL
_GetCurrentThread = kernel32.GetCurrentThread
_GetCurrentThread.argtypes = ()
_GetCurrentThread.restype = wintypes.HANDLE
_OpenThread = kernel32.OpenThread
_OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_OpenThread.restype = wintypes.HANDLE
_CloseHandle = kernel32.CloseHandle
_CloseHandle.argtypes = (wintypes.HANDLE,)
_CloseHandle.restype = wintypes.BOOL


def set_current_thread_priority(priority: int) -> bool:
    """Sets the calling thread's scheduling priority (THREAD_PRIORITY_*)."""
    return bool(_SetThreadPriority(_GetCurrentThread(), priority))


def set_thread_priority(native_id: Optional[int], priority: int) -> bool:
    """Sets another thread's priority by its OS thread id (threading.Thread.native_id)."""
    if not native_id:
        return False
    handle = _OpenThread(THREAD_SET_LIMITED_INFORMATION, False, native_id)
    if not handle:
        return False
    try:
        return bool(_SetThreadPriority(handle, priority))
    finally:
        _CloseHandle(handle)


# --- Display refresh rate -----------------------------------------------------------

class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32), ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD), ("dmPositionX", ctypes.c_long), ("dmPositionY", ctypes.c_long),
        ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short), ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
    ]


ENUM_CURRENT_SETTINGS = -1
_EnumDisplaySettingsW = user32.EnumDisplaySettingsW
_EnumDisplaySettingsW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DEVMODEW))
_EnumDisplaySettingsW.restype = wintypes.BOOL


def display_refresh_rate(device_name: Optional[str] = None) -> int:
    """Current refresh rate in Hz of a display by GDI device name (None = primary), or 0 if unknown."""
    mode = DEVMODEW()
    mode.dmSize = ctypes.sizeof(DEVMODEW)
    if not _EnumDisplaySettingsW(device_name, ctypes.c_uint32(ENUM_CURRENT_SETTINGS).value, ctypes.byref(mode)):
        return 0
    rate = int(mode.dmDisplayFrequency)
    return rate if rate > 1 else 0  # 0 and 1 mean "hardware default"
