"""
Process enumeration for audio routing (Toolhelp snapshots), via ctypes.

Kept apart from win32_api so the audio code doesn't depend on the window/overlay bindings.
"""
import ctypes
from ctypes import wintypes
import os
from typing import Dict, List, Optional, Tuple

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260

# Processes that never make sense as an audio source of their own.
IGNORED_EXES = {"system", "idle", "audiodg.exe", "svchost.exe", "csrss.exe", "dwm.exe", "explorer.exe"}


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


CreateToolhelp32Snapshot = kernel32.CreateToolhelp32Snapshot
CreateToolhelp32Snapshot.restype = wintypes.HANDLE
CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
Process32FirstW = kernel32.Process32FirstW
Process32FirstW.restype = wintypes.BOOL
Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
Process32NextW = kernel32.Process32NextW
Process32NextW.restype = wintypes.BOOL
Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
CloseHandle = kernel32.CloseHandle
CloseHandle.restype = wintypes.BOOL
CloseHandle.argtypes = (wintypes.HANDLE,)
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
GetWindowThreadProcessId.restype = wintypes.DWORD
GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))


def list_processes() -> List[Tuple[int, int, str]]:
    """(pid, parent pid, lower-case exe name) for every running process."""
    snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return []
    found: List[Tuple[int, int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            found.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile.lower()))
            ok = Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        CloseHandle(snapshot)
    return found


def window_process_id(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


class ProcessSnapshot:
    """One snapshot of the process table with the lookups audio routing needs."""

    def __init__(self, processes: Optional[List[Tuple[int, int, str]]] = None):
        self.processes = processes if processes is not None else list_processes()
        self.exe_of: Dict[int, str] = {pid: exe for pid, _, exe in self.processes}
        self.parent_of: Dict[int, int] = {pid: parent for pid, parent, _ in self.processes}
        self.own_pid = os.getpid()

    def root_of(self, pid: int) -> int:
        """Climbs to the topmost ancestor with the same exe (e.g. a browser's main process)."""
        exe = self.exe_of.get(pid)
        seen = {pid}
        while True:
            parent = self.parent_of.get(pid)
            if parent is None or parent in seen or parent == 0 or self.exe_of.get(parent) != exe:
                return pid
            seen.add(parent)
            pid = parent

    def roots_by_exe(self) -> Dict[str, List[int]]:
        """exe -> PIDs at the top of each process tree of that exe."""
        roots: Dict[str, List[int]] = {}
        for pid, parent, exe in self.processes:
            if pid in (0, 4) or pid == self.own_pid:
                continue
            if parent != pid and self.exe_of.get(parent) == exe:
                continue  # A child of the same app; covered by its root's process tree
            roots.setdefault(exe, []).append(pid)
        return roots
