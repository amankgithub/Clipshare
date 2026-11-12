"""Tray balloon "Received <name>"; clicking it opens Explorer with the file selected.

A hidden window on its own thread owns a notification icon that exists only while a balloon
is showing (ClipBridge has no permanent tray UI).
"""

from __future__ import annotations

import collections
import logging
import subprocess
import threading
from pathlib import Path

import win32api
import win32con
import win32gui

log = logging.getLogger(__name__)

WM_SHOW = win32con.WM_APP + 1
WM_TRAY = win32con.WM_APP + 2
NIN_BALLOONHIDE = win32con.WM_USER + 3
NIN_BALLOONTIMEOUT = win32con.WM_USER + 4
NIN_BALLOONUSERCLICK = win32con.WM_USER + 5
NIIF_INFO = 0x1
ICON_ID = 1


class BalloonThread(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="files-balloon", daemon=True)
        self._hwnd = 0
        self._ready = threading.Event()
        self._inbox: collections.deque[Path] = collections.deque()
        self._shown: Path | None = None
        self._icon_added = False

    # -- public (any thread) ----------------------------------------------
    def start_and_wait(self, timeout: float = 10.0) -> None:
        self.start()
        if not self._ready.wait(timeout) or not self._hwnd:
            raise RuntimeError("balloon thread failed to start")

    def show(self, path: Path) -> None:
        self._inbox.append(path)
        win32gui.PostMessage(self._hwnd, WM_SHOW, 0, 0)

    def stop(self) -> None:
        if self._hwnd:
            win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)

    # -- thread body --------------------------------------------------------
    def run(self) -> None:
        try:
            hinst = win32api.GetModuleHandle(None)
            wc = win32gui.WNDCLASS()
            wc.hInstance = hinst
            wc.lpszClassName = "ClipBridgeBalloonWindow"
            wc.lpfnWndProc = self._wndproc
            atom = win32gui.RegisterClass(wc)
            self._hwnd = win32gui.CreateWindowEx(0, atom, "ClipBridge", 0, 0, 0, 0, 0, 0, 0, hinst, None)
        except Exception:
            log.exception("balloon window creation failed")
            self._ready.set()
            return
        self._ready.set()
        win32gui.PumpMessages()

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        try:
            if msg == WM_SHOW:
                while self._inbox:
                    self._show(self._inbox.popleft())
                return 0
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event == NIN_BALLOONUSERCLICK and self._shown is not None:
                    _reveal(self._shown)
                if event in (NIN_BALLOONUSERCLICK, NIN_BALLOONTIMEOUT, NIN_BALLOONHIDE):
                    self._remove_icon()
                return 0
            if msg == win32con.WM_CLOSE:
                self._remove_icon()
                win32gui.DestroyWindow(hwnd)
                return 0
            if msg == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("balloon window error")
        return int(win32gui.DefWindowProc(hwnd, msg, wparam, lparam))

    def _show(self, path: Path) -> None:
        icon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        if not self._icon_added:
            flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, (self._hwnd, ICON_ID, flags, WM_TRAY, icon, "ClipBridge"))
            self._icon_added = True
        self._shown = path
        win32gui.Shell_NotifyIcon(
            win32gui.NIM_MODIFY,
            (self._hwnd, ICON_ID, win32gui.NIF_INFO, WM_TRAY, icon, "ClipBridge",
             f"Received {path.name}", 10, "ClipBridge", NIIF_INFO),
        )

    def _remove_icon(self) -> None:
        if self._icon_added:
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self._hwnd, ICON_ID))
            except Exception:
                log.debug("removing tray icon failed")
            self._icon_added = False


def _reveal(path: Path) -> None:
    """Open Explorer with the file selected."""
    subprocess.Popen(f'explorer /select,"{path}"')
