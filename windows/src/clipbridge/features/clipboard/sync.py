"""SyncEngine: pure echo-suppression / pending-clip rules (docs/PLAN.md "Sync rules")."""

from __future__ import annotations

import hashlib


def text_hash(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


class SyncEngine:
    def __init__(self) -> None:
        self._last_hash: bytes | None = None
        self._pending: str | None = None

    def on_local(self, text: str, connected: bool) -> str | None:
        """Local clipboard changed. Return text to send now, else None (dropped or queued)."""
        digest = text_hash(text)
        if digest == self._last_hash:
            return None
        self._last_hash = digest
        if connected:
            self._pending = None
            return text
        self._pending = text
        return None

    def on_remote(self, text: str) -> bool:
        """Remote clip arrived. Return True if it should be written to the local clipboard."""
        digest = text_hash(text)
        if digest == self._last_hash:
            return False
        self._last_hash = digest
        self._pending = None
        return True

    def take_pending(self) -> str | None:
        """On (re)connect: the single unsent local clip, if any."""
        text, self._pending = self._pending, None
        return text
