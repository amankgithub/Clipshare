"""Hello (PROTOCOL.md section 6.1): who the peer is and which features, at which versions, it handles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from clipbridge.link.envelope import LINK_CH

MAX_NAME = 64
MAX_FEATURES = 64
MAX_VERSION = 2**31 - 1
_FEATURE_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")


@dataclass(frozen=True)
class Hello:
    name: str
    features: dict[str, int]


def valid_feature_name(name: object) -> bool:
    return isinstance(name, str) and _FEATURE_NAME.fullmatch(name) is not None and not name.startswith(LINK_CH)


def valid_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and 1 <= len(name) <= MAX_NAME
        and not any(ord(c) < 0x20 or ord(c) == 0x7F for c in name)
    )


def clean_name(name: str) -> str:
    """A valid hello name made from `name` (for example the PC's host name)."""
    cleaned = "".join(c for c in name if ord(c) >= 0x20 and ord(c) != 0x7F)[:MAX_NAME]
    return cleaned or "ClipBridge"


def encode(name: str, features: Mapping[str, int]) -> dict[str, Any]:
    return {"ch": LINK_CH, "type": "hello", "name": name, "features": dict(features)}


def is_hello(header: dict[str, Any]) -> bool:
    return header.get("ch") == LINK_CH and header.get("type") == "hello"


def parse(header: dict[str, Any]) -> Hello | None:
    """The hello in `header`, or None if it is not a valid hello (close 4400)."""
    if not is_hello(header):
        return None
    name, features = header.get("name"), header.get("features")
    if not valid_name(name) or not isinstance(features, dict) or len(features) > MAX_FEATURES:
        return None
    for key, version in features.items():
        if not valid_feature_name(key):
            return None
        if not isinstance(version, int) or isinstance(version, bool) or not 1 <= version <= MAX_VERSION:
            return None
    assert isinstance(name, str)
    return Hello(name, dict(features))
