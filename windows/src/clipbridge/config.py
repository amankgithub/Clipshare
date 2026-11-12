"""Config in %APPDATA%\\ClipBridge\\config.json. The PSK is stored DPAPI-encrypted (current user)."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import win32crypt

PSK_LEN = 32


def config_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "ClipBridge"


def log_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "ClipBridge" / "logs"


def _config_path() -> Path:
    return config_dir() / "config.json"


def load_psk() -> bytes | None:
    path = _config_path()
    if not path.exists():
        return None
    blob = base64.b64decode(json.loads(path.read_text("utf-8"))["psk_dpapi"])
    psk = win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]
    if len(psk) != PSK_LEN:
        raise ValueError("stored PSK has wrong length")
    return bytes(psk)


def create_psk() -> bytes:
    psk = os.urandom(PSK_LEN)
    blob = win32crypt.CryptProtectData(psk, "ClipBridge PSK", None, None, None, 0)
    config_dir().mkdir(parents=True, exist_ok=True)
    data = {"psk_dpapi": base64.b64encode(blob).decode("ascii")}
    _config_path().write_text(json.dumps(data), "utf-8")
    return psk


def get_or_create_psk() -> bytes:
    return load_psk() or create_psk()
