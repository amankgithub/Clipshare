"""SyncEngine: pure echo-suppression / pending-clip rules (docs/PLAN.md "Sync rules")."""

from __future__ import annotations

import hashlib
from typing import NamedTuple


class Clip(NamedTuple):
    """kind "text": data is str. kind "image": data is PNG bytes."""

    kind: str
    data: str | bytes


def clip_hash(clip: Clip) -> bytes:
    raw = clip.data.encode("utf-8") if isinstance(clip.data, str) else clip.data
    return hashlib.sha256(raw).digest()


def text_hash(text: str) -> bytes:
    return clip_hash(Clip("text", text))


class SyncEngine:
    def __init__(self) -> None:
        self._last_hash: bytes | None = None  # shared by text and images
        self._pending: Clip | None = None

    def on_local(self, clip: Clip, connected: bool) -> Clip | None:
        """Local clipboard changed. Return clip to send now, else None (dropped or queued)."""
        digest = clip_hash(clip)
        if digest == self._last_hash:
            return None
        self._last_hash = digest
        if connected:
            self._pending = None
            return clip
        self._pending = clip
        return None

    def on_remote(self, clip: Clip) -> bool:
        """Remote clip arrived. Return True if it should be written to the local clipboard."""
        digest = clip_hash(clip)
        if digest == self._last_hash:
            return False
        self._last_hash = digest
        self._pending = None
        return True

    def take_pending(self) -> Clip | None:
        """On (re)connect: the single unsent local clip, if any."""
        clip, self._pending = self._pending, None
        return clip
