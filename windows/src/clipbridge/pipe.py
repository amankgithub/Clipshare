r"""CLI-to-server commands over `\\.\pipe\ClipBridge`: `clipbridge send` and `clipbridge unpair`.

One request per client connection: a UTF-8 JSON message, either `{"send": ["<absolute path>", ...]}`
(queue files for the phone) or `{"unpair": "<device>"}` (drop that device's live connections),
answered with a one-byte ack once the server has taken it. The pipe is local only (remote clients
rejected) and its DACL admits only the user the server runs as.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable, Sequence, Union

import pywintypes
import win32api
import win32con
import win32file
import win32pipe
import win32security
import winerror

log = logging.getLogger(__name__)

PIPE_NAME = r"\\.\pipe\ClipBridge"
BUFSIZE = 64 * 1024
ACK = b"\x01"
CONNECT_TIMEOUT_MS = 5000
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000  # not exported by pywin32


class ServerNotRunning(Exception):
    """No ClipBridge server is listening on the pipe."""


# -- pure ------------------------------------------------------------------
def validate_paths(args: Sequence[str], cwd: Path) -> tuple[list[Path], list[str]]:
    """Absolute paths of existing files, plus one error line per rejected argument."""
    files: list[Path] = []
    errors: list[str] = []
    for arg in args:
        path = Path(arg)
        if not path.is_absolute():
            path = cwd / path
        if path.is_dir():
            errors.append(f"{arg}: is a folder (only files can be sent)")
        elif not path.is_file():
            errors.append(f"{arg}: no such file")
        else:
            files.append(path)
    return files, errors


@dataclass(frozen=True)
class Send:
    paths: list[Path]


@dataclass(frozen=True)
class Unpair:
    device: str


Request = Union[Send, Unpair]


def encode_request(request: Request) -> bytes:
    if isinstance(request, Send):
        return json.dumps({"send": [str(p) for p in request.paths]}).encode("utf-8")
    return json.dumps({"unpair": request.device}).encode("utf-8")


def decode_request(data: bytes) -> Request:
    """The request; ValueError if it is malformed."""
    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError("malformed request") from e
    if not isinstance(message, dict) or len(message) != 1:
        raise ValueError("malformed request")
    paths, device = message.get("send"), message.get("unpair")
    if isinstance(paths, list) and all(isinstance(p, str) for p in paths):
        return Send([Path(p) for p in paths])
    if isinstance(device, str):
        return Unpair(device)
    raise ValueError("malformed request")


# -- client ----------------------------------------------------------------
def send_request(request: Request) -> None:
    """Hand `request` to the server; raises ServerNotRunning if nothing listens."""
    deadline = time.monotonic() + CONNECT_TIMEOUT_MS / 1000
    while True:
        try:
            handle = win32file.CreateFile(
                PIPE_NAME, win32con.GENERIC_READ | win32con.GENERIC_WRITE, 0, None, win32con.OPEN_EXISTING, 0, None
            )
            break
        except pywintypes.error as e:
            if e.winerror == winerror.ERROR_FILE_NOT_FOUND:
                raise ServerNotRunning from e
            if e.winerror != winerror.ERROR_PIPE_BUSY or time.monotonic() > deadline:
                raise
            try:
                win32pipe.WaitNamedPipe(PIPE_NAME, 1000)
            except pywintypes.error:
                pass
    try:
        win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        win32file.WriteFile(handle, encode_request(request))
        win32file.ReadFile(handle, 1)
    finally:
        win32file.CloseHandle(handle)


# -- server ----------------------------------------------------------------
def _current_user_attributes() -> win32security.SECURITY_ATTRIBUTES:
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, sid)
    sd = win32security.SECURITY_DESCRIPTOR()
    sd.SetSecurityDescriptorOwner(sid, False)
    sd.SetSecurityDescriptorDacl(True, dacl, False)
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = sd
    return attributes


def _read_message(handle: Any) -> bytes:
    data = b""
    while True:
        hr, chunk = win32file.ReadFile(handle, BUFSIZE)
        data += chunk
        if hr != winerror.ERROR_MORE_DATA:
            return data


class PipeServer(threading.Thread):
    """Accepts requests and passes each one to `on_request` (called on this thread)."""

    def __init__(self, on_request: Callable[[Request], None]) -> None:
        super().__init__(name="pipe", daemon=True)
        self._on_request = on_request
        self._stopping = False
        self._attributes = _current_user_attributes()
        self._first = self._create(first=True)  # fails now if another process owns the name

    def _create(self, first: bool = False) -> Any:
        open_mode = win32pipe.PIPE_ACCESS_DUPLEX | (FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0)
        pipe_mode = (
            win32pipe.PIPE_TYPE_MESSAGE
            | win32pipe.PIPE_READMODE_MESSAGE
            | win32pipe.PIPE_WAIT
            | win32pipe.PIPE_REJECT_REMOTE_CLIENTS
        )
        return win32pipe.CreateNamedPipe(  # a PyHANDLE: keep the object, it closes when collected
            PIPE_NAME, open_mode, pipe_mode, win32pipe.PIPE_UNLIMITED_INSTANCES,
            BUFSIZE, BUFSIZE, 0, self._attributes,
        )

    def stop(self) -> None:
        self._stopping = True
        try:  # wake the blocked ConnectNamedPipe
            win32file.CloseHandle(
                win32file.CreateFile(PIPE_NAME, win32con.GENERIC_READ, 0, None, win32con.OPEN_EXISTING, 0, None)
            )
        except pywintypes.error:
            pass

    def run(self) -> None:
        handle = self._first
        while True:
            try:
                win32pipe.ConnectNamedPipe(handle, None)
            except pywintypes.error as e:
                if e.winerror != winerror.ERROR_PIPE_CONNECTED:
                    log.warning("pipe connect failed: %s", e.strerror)
            if self._stopping:
                win32file.CloseHandle(handle)
                return
            try:
                current, handle = handle, self._create()  # keep one instance listening
            except pywintypes.error:
                log.exception("creating pipe instance failed; CLI requests disabled")
                win32file.CloseHandle(handle)
                return
            self._serve(current)

    def _serve(self, handle: Any) -> None:
        try:
            self._on_request(decode_request(_read_message(handle)))
            win32file.WriteFile(handle, ACK)
            win32file.FlushFileBuffers(handle)  # wait for the client to read the ack
        except ValueError:
            log.warning("ignoring malformed pipe request")
        except pywintypes.error as e:
            log.warning("pipe request failed: %s", e.strerror)
        except Exception:
            log.exception("pipe request failed")
        finally:
            try:
                win32pipe.DisconnectNamedPipe(handle)
            except pywintypes.error:
                pass
            win32file.CloseHandle(handle)
