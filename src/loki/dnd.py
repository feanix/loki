"""Win32 file drop onto a window (WM_DROPFILES) through ctypes. No Tk import, Windows only.

Why not tkdnd: no extra binary to bundle in the one-file exe. How: DragAcceptFiles marks a window as
a drop target and we subclass its window procedure to catch WM_DROPFILES. `on_drop` runs inside the
window procedure on the thread that owns the window, so it must only queue the paths.
`post_drop` builds a real HDROP and posts the same message, so tests and the exe self-test go through
the exact code an Explorer drop does.

All handles are 64-bit: every foreign call below sets argtypes/restype, because ctypes' default int
conversion truncates pointers (the same trap as in gui.capture_window).
"""

from __future__ import annotations

import ctypes
import logging
import struct
from collections.abc import Callable
from ctypes import wintypes

WM_DROPFILES = 0x0233
WM_COPYDATA = 0x004A
WM_COPYGLOBALDATA = 0x0049  # sent by Explorer alongside a drop on newer Windows
_PASS_THROUGH = (WM_DROPFILES, WM_COPYDATA, WM_COPYGLOBALDATA)
_GWLP_WNDPROC = -4
_MSGFLT_ALLOW = 1
_GMEM_MOVEABLE_ZERO = 0x0042

_LRESULT = ctypes.c_ssize_t
log = logging.getLogger(__name__)
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

_api = None
_live: list[DropTarget] = []  # keeps every callback alive while its window can still call it


def _load():
    """Declare the foreign signatures once (lazily, so importing this module is safe anywhere)."""
    global _api
    if _api is not None:
        return _api
    user32, shell32, kernel32 = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
    set_proc = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    get_proc = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    for fn, res, args in (
        (set_proc, ctypes.c_void_p, [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]),
        (get_proc, ctypes.c_void_p, [wintypes.HWND, ctypes.c_int]),
        (
            user32.CallWindowProcW,
            _LRESULT,
            [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM],
        ),
        (user32.IsWindow, wintypes.BOOL, [wintypes.HWND]),
        (user32.PostMessageW, wintypes.BOOL, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
        (
            user32.ChangeWindowMessageFilterEx,
            wintypes.BOOL,
            [wintypes.HWND, wintypes.UINT, wintypes.DWORD, ctypes.c_void_p],
        ),
        (shell32.DragAcceptFiles, None, [wintypes.HWND, wintypes.BOOL]),
        (shell32.DragQueryFileW, wintypes.UINT, [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]),
        (shell32.DragFinish, None, [ctypes.c_void_p]),
        (kernel32.GlobalAlloc, ctypes.c_void_p, [wintypes.UINT, ctypes.c_size_t]),
        (kernel32.GlobalLock, ctypes.c_void_p, [ctypes.c_void_p]),
        (kernel32.GlobalUnlock, wintypes.BOOL, [ctypes.c_void_p]),
        (kernel32.GlobalFree, ctypes.c_void_p, [ctypes.c_void_p]),
    ):
        fn.restype, fn.argtypes = res, args
    _api = (user32, shell32, kernel32, set_proc, get_proc)
    return _api


def read_drop(hdrop: int) -> list[str]:
    """Every path in an HDROP, then release it (DragFinish)."""
    _user32, shell32, _k, _s, _g = _load()
    try:
        count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
        paths = []
        for i in range(count):
            n = shell32.DragQueryFileW(hdrop, i, None, 0)
            buf = ctypes.create_unicode_buffer(n + 1)
            shell32.DragQueryFileW(hdrop, i, buf, n + 1)
            paths.append(buf.value)
        return paths
    finally:
        shell32.DragFinish(hdrop)


class DropTarget:
    """One subclassed window. `uninstall()` puts the old window procedure back."""

    def __init__(self, hwnd: int, on_drop: Callable[[list[str]], None]):
        self.hwnd = hwnd
        self.on_drop = on_drop
        self._old = None
        self._proc = _WNDPROC(self._wndproc)  # held on self: a collected callback would crash the window

    def _wndproc(self, hwnd, msg, wparam, lparam):
        user32 = _load()[0]
        if msg == WM_DROPFILES:
            try:
                paths = read_drop(wparam)
                self.on_drop(paths)
            except Exception:  # an exception must never cross back into the OS
                log.exception("file drop failed")
            return 0
        return user32.CallWindowProcW(self._old, hwnd, msg, wparam, lparam)

    def _install(self) -> None:
        user32, shell32, _k, set_proc, _g = _load()
        for msg in _PASS_THROUGH:  # let an Explorer drop through UIPI when Loki runs elevated
            user32.ChangeWindowMessageFilterEx(self.hwnd, msg, _MSGFLT_ALLOW, None)
        shell32.DragAcceptFiles(self.hwnd, True)
        self._old = set_proc(self.hwnd, _GWLP_WNDPROC, ctypes.cast(self._proc, ctypes.c_void_p))
        _live.append(self)

    def uninstall(self) -> None:
        if self not in _live:
            return
        user32, shell32, _k, set_proc, get_proc = _load()
        if user32.IsWindow(self.hwnd):
            shell32.DragAcceptFiles(self.hwnd, False)
            # Only unhook when we are still the outermost procedure; otherwise a later subclass
            # would be cut out of the chain, so leave ours in place (it just passes messages on).
            if get_proc(self.hwnd, _GWLP_WNDPROC) == ctypes.cast(self._proc, ctypes.c_void_p).value:
                set_proc(self.hwnd, _GWLP_WNDPROC, self._old)
                _live.remove(self)
                return
        # Window gone or re-subclassed: keep the callback alive, it still forwards messages.


def install(hwnd: int, on_drop: Callable[[list[str]], None]) -> DropTarget:
    target = DropTarget(int(hwnd), on_drop)
    target._install()
    return target


def post_drop(hwnd: int, paths: list[str]) -> None:
    """Post a real WM_DROPFILES (a genuine HDROP) to `hwnd`, as Explorer would."""
    user32, _s, kernel32, _set, _get = _load()
    blob = "".join(p + "\0" for p in paths) + "\0"
    wide = blob.encode("utf-16-le")
    # DROPFILES: DWORD pFiles (offset of the list), POINT pt, BOOL fNC, BOOL fWide.
    head = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    data = head + wide
    hmem = kernel32.GlobalAlloc(_GMEM_MOVEABLE_ZERO, len(data))
    if not hmem:
        raise OSError("GlobalAlloc failed")
    ptr = kernel32.GlobalLock(hmem)
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(hmem)
    if not user32.PostMessageW(int(hwnd), WM_DROPFILES, hmem, 0):
        kernel32.GlobalFree(hmem)  # not posted, so nobody will DragFinish it
        raise OSError("PostMessageW failed")
