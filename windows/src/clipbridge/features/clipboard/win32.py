"""Win32 clipboard thread: message-only window + clipboard listener. All clipboard I/O happens here."""

from __future__ import annotations

import collections
import ctypes
import logging
import threading
import time
from typing import Callable

import pywintypes
import win32api
import win32clipboard
import win32con
import win32gui

log = logging.getLogger(__name__)

WM_CLIPBOARDUPDATE = 0x031D
WM_SET_TEXT = win32con.WM_APP + 1
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
HWND_MESSAGE = -3
EXCLUDE_FORMAT = "ExcludeClipboardContentFromMonitorProcessing"
OPEN_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.5)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.RegisterSuspendResumeNotification.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
_user32.RegisterSuspendResumeNotification.restype = ctypes.c_void_p
_user32.AddClipboardFormatListener.argtypes = [ctypes.c_void_p]
_user32.AddClipboardFormatListener.restype = ctypes.c_int


class ClipboardThread(threading.Thread):
    def __init__(self, on_text: Callable[[str], None], on_resume: Callable[[], None]) -> None:
        super().__init__(name="clipboard-win32", daemon=True)
        self._on_text = on_text
        self._on_resume = on_resume
        self._outbox: collections.deque[str] = collections.deque()
        self._hwnd = 0
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._exclude_fmt = 0

    # -- public (any thread) ----------------------------------------------
    def start_and_wait(self, timeout: float = 10.0) -> None:
        self.start()
        if not self._ready.wait(timeout) or self._error is not None:
            raise RuntimeError(f"clipboard thread failed to start: {self._error}")

    def set_text(self, text: str) -> None:
        self._outbox.append(text)
        win32gui.PostMessage(self._hwnd, WM_SET_TEXT, 0, 0)

    def stop(self) -> None:
        if self._hwnd:
            win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)

    # -- thread body --------------------------------------------------------
    def run(self) -> None:
        try:
            self._create_window()
        except BaseException as exc:  # reported via start_and_wait
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        win32gui.PumpMessages()

    def _create_window(self) -> None:
        self._exclude_fmt = win32clipboard.RegisterClipboardFormat(EXCLUDE_FORMAT)
        hinst = win32api.GetModuleHandle(None)
        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpszClassName = "ClipBridgeClipboardWindow"
        wc.lpfnWndProc = self._wndproc
        atom = win32gui.RegisterClass(wc)
        self._hwnd = win32gui.CreateWindowEx(
            0, atom, "ClipBridge", 0, 0, 0, 0, 0, HWND_MESSAGE, 0, hinst, None
        )
        if not _user32.AddClipboardFormatListener(self._hwnd):
            raise ctypes.WinError(ctypes.get_last_error())
        if not _user32.RegisterSuspendResumeNotification(self._hwnd, 0):  # DEVICE_NOTIFY_WINDOW_HANDLE
            log.warning("RegisterSuspendResumeNotification failed; relying on the IP poll")

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        try:
            if msg == WM_CLIPBOARDUPDATE:
                self._handle_update()
                return 0
            if msg == WM_SET_TEXT:
                self._handle_set()
                return 0
            if msg == win32con.WM_POWERBROADCAST:
                if wparam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                    log.info("system resume")
                    self._on_resume()
                return 1  # TRUE
            if msg == win32con.WM_CLOSE:
                win32gui.DestroyWindow(hwnd)
                return 0
            if msg == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("clipboard window handler error")
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    # -- clipboard I/O (this thread only) ----------------------------------
    def _open(self) -> bool:
        for delay in OPEN_RETRY_DELAYS:
            try:
                win32clipboard.OpenClipboard(self._hwnd)
                return True
            except pywintypes.error:
                time.sleep(delay)
        log.warning("OpenClipboard failed after retries")
        return False

    def _handle_update(self) -> None:
        if win32clipboard.IsClipboardFormatAvailable(self._exclude_fmt):
            log.debug("clip skipped (excluded from monitoring)")
            return
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return
        if not self._open():
            return
        try:
            text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        except pywintypes.error:
            return
        finally:
            win32clipboard.CloseClipboard()
        if isinstance(text, str) and text:
            self._on_text(text)

    def _handle_set(self) -> None:
        while self._outbox:
            text = self._outbox.popleft()
            if not self._open():
                continue
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
            except pywintypes.error:
                log.exception("set clipboard failed")
            finally:
                win32clipboard.CloseClipboard()
