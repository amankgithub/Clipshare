"""Plaintext handshake messages (PROTOCOL.md section 3). Pure encode/parse."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass

VERSION = 1
MAX_HANDSHAKE_BYTES = 4096
CLOSE_PROTOCOL = 4400
CLOSE_AUTH = 4401
CLOSE_UNKNOWN_TRANSFER = 4404
CLOSE_TIMEOUT = 4408
CLOSE_REPLACED = 4409
CLOSE_VERSION = 4426
CLOSE_FRAME = 4402

_DEVICE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class HandshakeError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Auth:
    cn: bytes
    device: str
    mac: bytes


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(value: object, length: int) -> bytes:
    if not isinstance(value, str):
        raise HandshakeError(CLOSE_PROTOCOL)
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise HandshakeError(CLOSE_PROTOCOL) from exc
    if len(raw) != length:
        raise HandshakeError(CLOSE_PROTOCOL)
    return raw


def challenge(sn: bytes) -> str:
    return json.dumps({"type": "challenge", "v": VERSION, "sn": b64e(sn)})


def auth_ok(mac: bytes) -> str:
    return json.dumps({"type": "auth_ok", "mac": b64e(mac)})


def parse_auth(message: str | bytes) -> Auth:
    if not isinstance(message, str) or len(message.encode("utf-8")) > MAX_HANDSHAKE_BYTES:
        raise HandshakeError(CLOSE_PROTOCOL)
    try:
        obj = json.loads(message)
    except ValueError as exc:
        raise HandshakeError(CLOSE_PROTOCOL) from exc
    if not isinstance(obj, dict) or obj.get("type") != "auth":
        raise HandshakeError(CLOSE_PROTOCOL)
    version = obj.get("v")
    if not isinstance(version, int) or isinstance(version, bool):
        raise HandshakeError(CLOSE_PROTOCOL)
    if version != VERSION:
        raise HandshakeError(CLOSE_VERSION)
    device = obj.get("device")
    if not isinstance(device, str) or not _DEVICE_RE.match(device):
        raise HandshakeError(CLOSE_PROTOCOL)
    return Auth(cn=b64d(obj.get("cn"), 16), device=device, mac=b64d(obj.get("mac"), 32))
