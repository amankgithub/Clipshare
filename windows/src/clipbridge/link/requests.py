"""Requests and replies (PROTOCOL.md section 6.2): `mid` validation, the reply message, pending requests."""

from __future__ import annotations

import asyncio
import itertools
import re
from dataclasses import dataclass, field
from typing import Any

from clipbridge.link.envelope import LINK_CH

REQUEST_TIMEOUT = 10.0  # no reply within this = not delivered
_MID = re.compile(r"[A-Za-z0-9._-]{1,64}")


@dataclass(frozen=True)
class Answer:
    """Reply content a feature returns from `on_message` (both parts optional)."""

    data: dict[str, Any] | None = None
    body: bytes = b""


@dataclass(frozen=True)
class Reply:
    """A peer's reply to a request: `ok` false means it got the message but did not handle it."""

    ok: bool
    data: dict[str, Any] | None = None
    body: bytes = b""


def valid_mid(mid: object) -> bool:
    return isinstance(mid, str) and _MID.fullmatch(mid) is not None


def requested_mid(header: dict[str, Any]) -> str | None:
    """The request id a feature message carries, or None (absent or invalid)."""
    mid = header.get("mid")
    return mid if valid_mid(mid) else None  # type: ignore[return-value]


def reply(mid: str, ok: bool, answer: Answer | None = None) -> tuple[dict[str, Any], bytes]:
    header: dict[str, Any] = {"ch": LINK_CH, "type": "reply", "mid": mid, "ok": ok}
    if ok and answer is not None:
        if answer.data is not None:
            header["data"] = answer.data
        return header, answer.body
    return header, b""


def parse_reply(header: dict[str, Any], body: bytes) -> tuple[str, Reply] | None:
    """(mid, reply) of a `reply` message, or None if it is malformed."""
    mid, ok, data = header.get("mid"), header.get("ok"), header.get("data")
    if not valid_mid(mid) or not isinstance(ok, bool) or not (data is None or isinstance(data, dict)):
        return None
    assert isinstance(mid, str)
    return mid, Reply(ok, data, body)


@dataclass
class PendingRequests:
    """This side's unanswered requests on one connection."""

    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    _waiting: dict[str, asyncio.Future[Reply | None]] = field(default_factory=dict)

    def open(self) -> tuple[str, asyncio.Future[Reply | None]]:
        mid = f"s-{next(self._ids)}"
        future: asyncio.Future[Reply | None] = asyncio.get_running_loop().create_future()
        self._waiting[mid] = future
        return mid, future

    def close(self, mid: str) -> None:
        self._waiting.pop(mid, None)

    def resolve(self, mid: str, result: Reply) -> None:
        """Complete the request `mid`; a reply for an unknown `mid` is ignored."""
        future = self._waiting.pop(mid, None)
        if future is not None and not future.done():
            future.set_result(result)

    def fail_all(self) -> None:
        """The connection closed: every unanswered request counts as not delivered."""
        for future in self._waiting.values():
            if not future.done():
                future.set_result(None)
        self._waiting.clear()
