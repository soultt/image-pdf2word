"""Single-instance coordination for the Windows GUI.

Kept as a top-level module (outside the ``pdf2word`` package) so a second
launch can check the mutex and exit without importing numpy/torch/ONNX.

A named mutex detects an existing instance; a named event asks that instance
to restore and focus its window. All operations are no-ops off Windows.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import sys
from typing import Optional

ERROR_ALREADY_EXISTS = 183
INFINITE = 0xFFFFFFFF
EVENT_MODIFY_STATE = 0x0002


class SingleInstance:
    def __init__(self, name: str = "pdf2word") -> None:
        self._available = sys.platform == "win32"
        self._k = ctypes.windll.kernel32 if self._available else None
        # Separate installations / portable copies must not share the mutex.
        app_dir = os.environ.get("PDF2WORD_APP_DIR") or os.path.dirname(
            os.path.abspath(sys.argv[0])
        )
        suffix = hashlib.md5(app_dir.encode("utf-8", "ignore")).hexdigest()[:8]
        prefix = "Local\\{}_{}".format(name, suffix)
        self._mutex_name = prefix + "_mutex"
        self._event_name = prefix + "_show"
        self._mutex: Optional[int] = None
        self._event: Optional[int] = None

    def acquire(self) -> bool:
        """Return True when this is the first instance (mutex kept open)."""
        if not self._available:
            return True
        k = self._k
        k.CreateMutexW.restype = ctypes.c_void_p
        k.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        k.GetLastError.restype = ctypes.c_uint32
        handle = k.CreateMutexW(None, True, self._mutex_name)
        if not handle:
            return True
        if k.GetLastError() == ERROR_ALREADY_EXISTS:
            k.CloseHandle(ctypes.c_void_p(handle))
            return False
        self._mutex = handle
        return True

    def create_show_event(self) -> None:
        """Create the event other instances signal (first instance only)."""
        if not self._available:
            return
        k = self._k
        k.CreateEventW.restype = ctypes.c_void_p
        k.CreateEventW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        )
        # Auto-reset event: each signal wakes the one watcher thread.
        self._event = k.CreateEventW(None, False, False, self._event_name)

    def signal_show(self) -> bool:
        """Ask the running instance to show its window (second instance)."""
        if not self._available:
            return False
        k = self._k
        k.OpenEventW.restype = ctypes.c_void_p
        k.OpenEventW.argtypes = (ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p)
        handle = k.OpenEventW(EVENT_MODIFY_STATE, False, self._event_name)
        if not handle:
            return False
        k.SetEvent.argtypes = (ctypes.c_void_p,)
        k.SetEvent(ctypes.c_void_p(handle))
        k.CloseHandle(ctypes.c_void_p(handle))
        return True

    def wait_for_show(self) -> None:
        """Block until another instance requests the window (watcher thread)."""
        if not self._available or not self._event:
            return
        k = self._k
        k.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        k.WaitForSingleObject.restype = ctypes.c_uint32
        k.WaitForSingleObject(ctypes.c_void_p(self._event), INFINITE)

    def close(self) -> None:
        """Release the mutex and event handles on a clean shutdown."""
        if not self._available:
            return
        k = self._k
        if self._event:
            k.CloseHandle(ctypes.c_void_p(self._event))
            self._event = None
        if self._mutex:
            k.ReleaseMutex.argtypes = (ctypes.c_void_p,)
            k.ReleaseMutex(ctypes.c_void_p(self._mutex))
            k.CloseHandle(ctypes.c_void_p(self._mutex))
            self._mutex = None
