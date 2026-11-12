"""Handshake MACs, HKDF session keys and AES-256-GCM frames (PROTOCOL.md sections 3-5). Pure, no I/O."""

from __future__ import annotations

import hashlib
import hmac

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_LEN = 12
TAG_LEN = 16
MIN_FRAME_LEN = NONCE_LEN + TAG_LEN
MAX_COUNTER = 2**64 - 1


class FrameError(Exception):
    """Frame failed an integrity check (close 4402)."""


class CounterExhausted(Exception):
    """Send counter would exceed 2^64 - 1; connection must be closed."""


def client_mac(psk: bytes, sn: bytes, cn: bytes, device: str) -> bytes:
    return hmac.new(psk, b"c" + sn + cn + device.encode("utf-8"), hashlib.sha256).digest()


def server_mac(psk: bytes, sn: bytes, cn: bytes) -> bytes:
    return hmac.new(psk, b"s" + sn + cn, hashlib.sha256).digest()


def _hkdf(psk: bytes, salt: bytes, info: bytes) -> bytes:
    prk = hmac.new(salt, psk, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()


def derive_keys(psk: bytes, sn: bytes, cn: bytes) -> tuple[bytes, bytes]:
    """Return (key_c2s, key_s2c)."""
    salt = sn + cn
    return _hkdf(psk, salt, b"c2s"), _hkdf(psk, salt, b"s2c")


def _nonce(counter: int) -> bytes:
    return counter.to_bytes(8, "little") + b"\x00\x00\x00\x00"


def seal(key: bytes, counter: int, plaintext: bytes) -> bytes:
    nonce = _nonce(counter)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def open_frame(key: bytes, frame: bytes, expected_counter: int) -> bytes:
    """Check order per spec: length, nonce padding, counter, GCM."""
    if len(frame) < MIN_FRAME_LEN:
        raise FrameError("short frame")
    nonce = frame[:NONCE_LEN]
    if nonce[8:] != b"\x00\x00\x00\x00":
        raise FrameError("bad nonce padding")
    if int.from_bytes(nonce[:8], "little") != expected_counter:
        raise FrameError("bad counter")
    try:
        return AESGCM(key).decrypt(nonce, frame[NONCE_LEN:], None)
    except InvalidTag as exc:
        raise FrameError("bad tag") from exc


class SecureChannel:
    """One direction pair of counters and keys for an established connection."""

    def __init__(self, send_key: bytes, recv_key: bytes) -> None:
        self._send_key = send_key
        self._recv_key = recv_key
        self._send_counter = 0
        self._recv_counter = 0

    def encrypt(self, plaintext: bytes) -> bytes:
        if self._send_counter > MAX_COUNTER:
            raise CounterExhausted
        frame = seal(self._send_key, self._send_counter, plaintext)
        self._send_counter += 1
        return frame

    def decrypt(self, frame: bytes) -> bytes:
        plaintext = open_frame(self._recv_key, frame, self._recv_counter)
        self._recv_counter += 1
        return plaintext
