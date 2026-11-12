"""Clipboard feature: glue between SyncEngine, the link, and a platform clipboard writer."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from clipbridge.features.clipboard.sync import SyncEngine, text_hash
from clipbridge.link.feature import Sender
from clipbridge.link.peers import Peer

log = logging.getLogger(__name__)


def _clean(text: str) -> str:
    """Drop lone surrogates (the wire format must not carry them)."""
    return text.encode("utf-8", "replace").decode("utf-8")


class ClipboardFeature:
    name = "clipboard"

    def __init__(
        self,
        link: Sender,
        write_text: Callable[[str], None],
        engine: SyncEngine | None = None,
    ) -> None:
        self._link = link
        self._write_text = write_text
        self._engine = engine or SyncEngine()

    @staticmethod
    def _header(text: str) -> dict[str, Any]:
        return {
            "ch": "clipboard",
            "type": "clip",
            "kind": "text",
            "data": text,
            "ts": int(time.time() * 1000),
        }

    async def on_local_text(self, text: str) -> None:
        """Local clipboard changed (must run on the event loop)."""
        text = _clean(text)
        if not text:
            return
        to_send = self._engine.on_local(text, connected=bool(self._link.peers))
        if to_send is None:
            log.debug("local clip dropped/queued (len=%d)", len(text))
            return
        log.info("local clip -> peers (len=%d, hash=%s)", len(text), text_hash(text).hex()[:8])
        await self._link.broadcast(self._header(to_send))

    async def on_peer_connected(self, peer: Peer) -> None:
        pending = self._engine.take_pending()
        if pending is not None:
            await self._link.send(peer, self._header(pending))

    async def on_peer_disconnected(self, peer: Peer) -> None:
        return None

    async def on_message(self, peer: Peer, header: dict[str, Any], body: bytes) -> None:
        if header.get("type") != "clip" or header.get("kind") != "text":
            return
        text = header.get("data")
        if not isinstance(text, str) or not text:
            return
        if not self._engine.on_remote(text):
            log.debug("remote clip dropped (echo), len=%d", len(text))
            return
        log.info("clip <- %s (len=%d, hash=%s)", peer.device, len(text), text_hash(text).hex()[:8])
        self._write_text(text)
        await self._link.broadcast(self._header(text), exclude=peer)
