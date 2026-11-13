"""Envelope: u32 BE header_len || header JSON (UTF-8) || body (PROTOCOL.md section 6)."""

from __future__ import annotations

import json
from typing import Any

MAX_PLAINTEXT = 16 * 1024 * 1024
LINK_CH = "link"  # `ch` of link-layer messages; every `ch` starting with it is reserved for the link


class EnvelopeError(Exception):
    """Malformed envelope: drop the message, keep the connection."""


class EnvelopeTooLarge(Exception):
    """Envelope exceeds MAX_PLAINTEXT: sender must skip it."""


def encode(header: dict[str, Any], body: bytes = b"") -> bytes:
    raw = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    total = 4 + len(raw) + len(body)
    if total > MAX_PLAINTEXT:
        raise EnvelopeTooLarge(total)
    return len(raw).to_bytes(4, "big") + raw + body


def decode(plaintext: bytes) -> tuple[dict[str, Any], bytes]:
    if len(plaintext) < 4:
        raise EnvelopeError("too short")
    header_len = int.from_bytes(plaintext[:4], "big")
    if header_len > len(plaintext) - 4:
        raise EnvelopeError("header_len exceeds plaintext")
    try:
        header = json.loads(plaintext[4 : 4 + header_len].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EnvelopeError("bad header") from exc
    if not isinstance(header, dict):
        raise EnvelopeError("header not an object")
    if not isinstance(header.get("ch"), str):
        raise EnvelopeError("missing ch")
    return header, plaintext[4 + header_len :]
