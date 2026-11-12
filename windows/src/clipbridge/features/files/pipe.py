r"""Send entry point: `clipbridge send <files>` hands paths to the running server over `\\.\pipe\ClipBridge`.

One request per client connection: a UTF-8 JSON message `{"send": ["<absolute path>", ...]}`,
answered with a one-byte ack once the server has queued the paths. The pipe is local only
(remote clients rejected) and its DACL admits only the user the server runs as.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

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


def encode_request(paths: Sequence[Path]) -> bytes:
    return json.dumps({"send": [str(p) for p in paths]}).encode("utf-8")


def decode_request(data: bytes) -> list[Path]:
    """Paths of a request; ValueError if it is malformed."""
    try:
        message = json.loads(data.decode("utf-8"))
        paths = message["send"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as e:
        raise ValueError("malformed request") from e
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ValueError("malformed request")
    return [Path(p) for p in paths]


# -- client ----------------------------------------------------------------
def send_request(paths: Sequence[Path]) -> None:
    """Hand `paths` to the server; raises ServerNotRunning if nothing listens."""
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
        win32file.WriteFile(handle, encode_request(paths))
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
    """Accepts send requests and passes each request's files to `on_paths` (called on this thread)."""

    def __init__(self, on_paths: Callable[[list[Path]], None]) -> None:
        super().__init__(name="files-pipe", daemon=True)
        self._on_paths = on_paths
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
                log.exception("creating pipe instance failed; send requests disabled")
                win32file.CloseHandle(handle)
                return
            self._serve(current)

    def _serve(self, handle: Any) -> None:
        try:
            paths = decode_request(_read_message(handle))
            self._on_paths(paths)
            win32file.WriteFile(handle, ACK)
            win32file.FlushFileBuffers(handle)  # wait for the client to read the ack
        except ValueError:
            log.warning("ignoring malformed send request")
        except pywintypes.error as e:
            log.warning("send request failed: %s", e.strerror)
        except Exception:
            log.exception("send request failed")
        finally:
            try:
                win32pipe.DisconnectNamedPipe(handle)
            except pywintypes.error:
                pass
            win32file.CloseHandle(handle)
