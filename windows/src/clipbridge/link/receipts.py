"""Delivery receipts (PROTOCOL.md section 6.1): `mid` validation and the ack message."""

from __future__ import annotations

import re
from typing import Any

LINK_CH = "link"
_MID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def requested_mid(header: dict[str, Any]) -> str | None:
    """The receipt id a feature message asks for, or None (absent or invalid)."""
    mid = header.get("mid")
    if isinstance(mid, str) and _MID.fullmatch(mid):
        return mid
    return None


def ack(mid: str) -> dict[str, Any]:
    return {"ch": LINK_CH, "type": "ack", "mid": mid}
