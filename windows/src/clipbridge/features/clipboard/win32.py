"""Win32 clipboard thread: message-only window + clipboard listener. All clipboard I/O happens here."""

from __future__ import annotations

import collections
import ctypes
import io
import logging
import struct
import threading
import time
from typing import Callable

import pywintypes
import win32api
import win32clipboard
import win32con
import win32gui
from PIL import Image

CF_DIBV5 = 17

log = logging.getLogger(__name__)

WM_CLIPBOARDUPDATE = 0x031D
WM_SET_TEXT = win32con.WM_APP + 1
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
HWND_MESSAGE = -3
EXCLUDE_FORMAT = "ExcludeClipboardContentFromMonitorProcessing"
OPTOUT_DWORD_FORMATS = ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard")  # DWORD 0 = do not sync
OPEN_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.5)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.RegisterSuspendResumeNotification.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
_user32.RegisterSuspendResumeNotification.restype = ctypes.c_void_p
_user32.AddClipboardFormatListener.argtypes = [ctypes.c_void_p]
_user32.AddClipboardFormatListener.restype = ctypes.c_int


class ClipboardThread(threading.Thread):
    def __init__(
        self,
        on_text: Callable[[str], None],
        on_resume: Callable[[], None],
        on_image: Callable[[bytes], None] | None = None,
    ) -> None:
        super().__init__(name="clipboard-win32", daemon=True)
        self._on_text = on_text
        self._on_image = on_image
        self._on_resume = on_resume
        self._png_fmt = 0
        self._outbox: collections.deque[tuple[str, str | bytes]] = collections.deque()
        self._hwnd = 0
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._exclude_fmt = 0
        self._optout_fmts: tuple[int, ...] = ()

    # -- public (any thread) ----------------------------------------------
    def start_and_wait(self, timeout: float = 10.0) -> None:
        self.start()
        if not self._ready.wait(timeout) or self._error is not None:
            raise RuntimeError(f"clipboard thread failed to start: {self._error}")

    def set_text(self, text: str) -> None:
        self._outbox.append(("text", text))
        win32gui.PostMessage(self._hwnd, WM_SET_TEXT, 0, 0)

    def set_image(self, png: bytes) -> None:
        self._outbox.append(("image", png))
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
        self._optout_fmts = tuple(win32clipboard.RegisterClipboardFormat(name) for name in OPTOUT_DWORD_FORMATS)
        self._png_fmt = win32clipboard.RegisterClipboardFormat("PNG")
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

    def _dword_is_zero(self, fmt: int) -> bool:
        """True if the clipboard carries registered format `fmt` with DWORD value 0 (an opt-out marker)."""
        if not win32clipboard.IsClipboardFormatAvailable(fmt):
            return False
        if not self._open():
            return False
        try:
            data = win32clipboard.GetClipboardData(fmt)
        except pywintypes.error:
            return False
        finally:
            win32clipboard.CloseClipboard()
        return isinstance(data, (bytes, bytearray)) and len(data) >= 4 and int.from_bytes(data[:4], "little") == 0

    def _handle_update(self) -> None:
        avail = win32clipboard.IsClipboardFormatAvailable
        if avail(self._exclude_fmt):
            log.debug("clip skipped (excluded from monitoring)")
            return
        if any(self._dword_is_zero(fmt) for fmt in self._optout_fmts):
            log.debug("clip skipped (no history / no cloud)")
            return
        if avail(win32con.CF_UNICODETEXT):
            fmt: int | None = win32con.CF_UNICODETEXT
        elif self._on_image is None:
            return
        elif avail(self._png_fmt):
            fmt = self._png_fmt
        elif avail(CF_DIBV5):
            fmt = CF_DIBV5
        elif avail(win32con.CF_DIB):
            fmt = win32con.CF_DIB
        else:
            return
        if not self._open():
            return
        try:
            data = win32clipboard.GetClipboardData(fmt)
        except pywintypes.error:
            return
        finally:
            win32clipboard.CloseClipboard()
        if fmt == win32con.CF_UNICODETEXT:
            if isinstance(data, str) and data:
                self._on_text(data)
            return
        if not isinstance(data, (bytes, bytearray)) or not data:
            return
        try:
            png = bytes(data) if fmt == self._png_fmt else dib_to_png(bytes(data))
        except Exception:
            log.warning("clipboard image conversion failed (len=%d)", len(data))
            return
        assert self._on_image is not None
        self._on_image(png)

    def _handle_set(self) -> None:
        while self._outbox:
            kind, data = self._outbox.popleft()
            try:
                dib = png_to_dib(data) if kind == "image" else b""  # type: ignore[arg-type]
            except Exception:
                log.warning("incoming image conversion failed (len=%d)", len(data))
                continue
            self._write(kind, data, dib)

    def _write(self, kind: str, data: str | bytes, dib: bytes) -> bool:
        """Replace the clipboard contents. Another process (clipboard history, the app that just copied) can
        disturb the write between EmptyClipboard and SetClipboardData, so the whole write is retried."""
        for delay in OPEN_RETRY_DELAYS:
            if not self._open():
                return False
            try:
                win32clipboard.EmptyClipboard()
                if kind == "image":
                    win32clipboard.SetClipboardData(self._png_fmt, data)
                    win32clipboard.SetClipboardData(win32con.CF_DIB, dib)
                else:
                    win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, data)
                return True
            except pywintypes.error as exc:
                error = exc
            finally:
                win32clipboard.CloseClipboard()
            time.sleep(delay)
        log.warning("set clipboard failed after retries: %s", error.strerror)
        return False


def dib_to_png(dib: bytes) -> bytes:
    """CF_DIB / CF_DIBV5 payload -> PNG bytes (prepend a BMP file header, let Pillow decode)."""
    hdr_size = struct.unpack_from("<I", dib, 0)[0]
    bit_count = struct.unpack_from("<H", dib, 14)[0]
    compression = struct.unpack_from("<I", dib, 16)[0]
    clr_used = struct.unpack_from("<I", dib, 32)[0]
    palette = clr_used * 4 if clr_used else (4 << bit_count if bit_count <= 8 else 0)
    masks = 12 if hdr_size == 40 and compression == 3 else 0  # BI_BITFIELDS
    offset = 14 + hdr_size + masks + palette
    bmp = b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, offset) + dib
    with Image.open(io.BytesIO(bmp)) as im:
        im.load()
        out = io.BytesIO()
        im.save(out, "PNG")
    return out.getvalue()


def png_to_dib(png: bytes) -> bytes:
    """PNG bytes -> CF_DIB payload (Pillow BMP minus the 14-byte file header; alpha flattened on white)."""
    with Image.open(io.BytesIO(png)) as im:
        im.load()
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.getchannel("A"))
            im = bg
        else:
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, "BMP")
    return out.getvalue()[14:]
