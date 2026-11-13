"""Config in %APPDATA%\\ClipBridge. The device registry is stored DPAPI-encrypted (current user)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import win32api
import win32crypt
import win32event

PSK_LEN = 32
LOCK_TIMEOUT_MS = 10_000


def config_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "ClipBridge"


def log_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "ClipBridge" / "logs"


def partial_dir() -> Path:
    """Unfinished incoming file transfers (`.part` + state)."""
    return Path(os.environ["LOCALAPPDATA"]) / "ClipBridge" / "partial"


def _protect(data: bytes) -> bytes:
    return bytes(win32crypt.CryptProtectData(data, "ClipBridge devices", None, None, None, 0))


def _unprotect(blob: bytes) -> bytes:
    return bytes(win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1])


@dataclass(frozen=True)
class Device:
    psk: bytes
    name: str  # latest hello name; empty until the device first connects


def new_device_id() -> str:
    return f"android-{secrets.token_hex(4)}"


class DeviceRegistry:
    """Paired devices `{device: {psk, name}}` (PROTOCOL.md section 2), in one DPAPI blob.

    Every call re-reads the file, so a running server sees `pair` / `unpair` from the CLI at once.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or config_dir() / "devices.bin"

    def load(self) -> dict[str, Device]:
        if not self._path.exists():
            return {}
        raw = json.loads(_unprotect(self._path.read_bytes()).decode("utf-8"))
        devices: dict[str, Device] = {}
        for device, entry in raw.items():
            try:
                psk = base64.b64decode(entry["psk"], validate=True)
            except (KeyError, TypeError, binascii.Error) as exc:
                raise ValueError(f"corrupt registry entry {device!r}") from exc
            if len(psk) != PSK_LEN:
                raise ValueError(f"registry entry {device!r} has a wrong-length PSK")
            devices[device] = Device(psk, str(entry.get("name", "")))
        return devices

    def _save(self, devices: dict[str, Device]) -> None:
        raw = {
            d: {"psk": base64.b64encode(e.psk).decode("ascii"), "name": e.name} for d, e in devices.items()
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp")
        try:
            tmp.write_bytes(_protect(json.dumps(raw).encode("utf-8")))
            os.replace(tmp, self._path)
        finally:
            tmp.unlink(missing_ok=True)

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize load-modify-save across threads and processes (server and CLI)."""
        digest = hashlib.sha256(str(self._path.resolve()).lower().encode("utf-8")).hexdigest()[:16]
        mutex = win32event.CreateMutex(None, False, f"Local\\ClipBridgeDevices-{digest}")
        try:
            if win32event.WaitForSingleObject(mutex, LOCK_TIMEOUT_MS) not in (
                win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED
            ):
                raise TimeoutError("device registry is locked")
            try:
                yield
            finally:
                win32event.ReleaseMutex(mutex)
        finally:
            win32api.CloseHandle(mutex)

    def pair(self, device: str) -> bytes:
        """Create (or re-key) `device`; returns its new PSK. An old key stops working at once."""
        with self.locked():
            devices = self.load()
            psk = os.urandom(PSK_LEN)
            old = devices.get(device)
            devices[device] = Device(psk, old.name if old else "")
            self._save(devices)
        return psk

    def unpair(self, device: str) -> bool:
        with self.locked():
            devices = self.load()
            if devices.pop(device, None) is None:
                return False
            self._save(devices)
        return True

    # -- what the link uses (link.server.Devices) ------------------------------
    def psk(self, device: str) -> bytes | None:
        entry = self.load().get(device)
        return entry.psk if entry else None

    def set_name(self, device: str, name: str) -> None:
        """Store `device`'s hello name; never re-creates an entry that was unpaired meanwhile."""
        with self.locked():
            devices = self.load()
            entry = devices.get(device)
            if entry is not None and entry.name != name:
                devices[device] = Device(entry.psk, name)
                self._save(devices)
