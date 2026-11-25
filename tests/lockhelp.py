"""Hold a file locked the way an exporter does: CreateFileW with share mode 0, so every other
open of the file fails with a sharing violation (WinError 32) until the handle is closed. Windows only."""

import ctypes
import os
import time
from contextlib import contextmanager
from ctypes import wintypes

import pytest

windows_only = pytest.mark.skipif(os.name != "nt", reason="share-mode locks are a Windows thing")

_GENERIC_READ = 0x80000000
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID = wintypes.HANDLE(-1).value


class Lock:
    """An open share-mode-0 handle on a file; `release()` is safe to call twice."""

    def __init__(self, path, share=0):
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]  # fmt: skip
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._close = k32.CloseHandle
        end = time.monotonic() + 3.0
        while True:  # a watcher thread may have the file open for a moment: wait for a gap, as a real exporter would
            handle = k32.CreateFileW(str(path), _GENERIC_READ, share, None, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None)
            if handle != _INVALID and handle is not None:
                break
            if ctypes.get_last_error() != 32 or time.monotonic() > end:
                raise ctypes.WinError(ctypes.get_last_error())
            time.sleep(0.001)
        self._handle = handle

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

    def release(self) -> None:
        if self._handle is not None:
            self._close(self._handle)
            self._handle = None


@contextmanager
def locked(path):
    lock = Lock(path)
    try:
        yield lock
    finally:
        lock.release()
