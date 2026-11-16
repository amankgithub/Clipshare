"""Clipboard feature: glue between SyncEngine, the link, and a platform clipboard writer."""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from clipbridge.features.clipboard.sync import Clip, SyncEngine, clip_hash
from clipbridge.link.envelope import MAX_PLAINTEXT
from clipbridge.link.feature import Sender
from clipbridge.link.peers import Peer

log = logging.getLogger(__name__)

PNG_MIME = "image/png"


def _clean(text: str) -> str:
    """Drop lone surrogates (the wire format must not carry them)."""
    return text.encode("utf-8", "replace").decode("utf-8")


def default_save_dir() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Pictures" / "ClipBridge"


def save_png(directory: Path, png: bytes, now: datetime | None = None) -> Path:
    """Write png to directory as clip_YYYYmmdd_HHMMSS.png (numeric suffix if taken)."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"clip_{(now or datetime.now()).strftime('%Y%m%d_%H%M%S')}"
    n = 0
    while True:
        path = directory / (f"{stem}.png" if n == 0 else f"{stem}_{n}.png")
        try:
            with open(path, "xb") as f:
                f.write(png)
            return path
        except FileExistsError:
            n += 1


class ClipboardFeature:
    name = "clipboard"
    version = 1

    def __init__(
        self,
        link: Sender,
        write_text: Callable[[str], None],
        write_image: Callable[[bytes], None] | None = None,
        engine: SyncEngine | None = None,
        save_dir: Path | None = None,
    ) -> None:
        self._link = link
        self._write_text = write_text
        self._write_image = write_image
        self._engine = engine or SyncEngine()
        self._save_dir = save_dir
        self._pending: dict[str, Clip] = {}  # device -> latest clip that device did not acknowledge
        self._latest: dict[str, int] = {}  # device -> seq of the newest clip sent to it
        self._seq = itertools.count(1)
        self._tasks: set[asyncio.Future[None]] = set()

    @staticmethod
    def _wire(clip: Clip) -> tuple[dict[str, Any], bytes]:
        ts = int(time.time() * 1000)
        if clip.kind == "image":
            assert isinstance(clip.data, bytes)
            return (
                {"ch": "clipboard", "type": "clip", "kind": "image", "mime": PNG_MIME, "ts": ts},
                clip.data,
            )
        return ({"ch": "clipboard", "type": "clip", "kind": "text", "data": clip.data, "ts": ts}, b"")

    @staticmethod
    def _too_big(clip: Clip) -> bool:
        return clip.kind == "image" and len(clip.data) + 256 > MAX_PLAINTEXT

    async def _send_all(self, clip: Clip, exclude: Peer | None = None) -> None:
        for peer in self._link.peers:
            if peer is not exclude and peer.supports(self.name):
                self._deliver_later(peer, clip)

    def _deliver_later(self, peer: Peer, clip: Clip) -> None:
        """Send `clip` to `peer` as a request in the background; keep it pending if undelivered."""
        self._pending.pop(peer.device, None)  # a newer clip replaces the pending one
        seq = next(self._seq)
        self._latest[peer.device] = seq
        task = asyncio.ensure_future(self._deliver(peer, clip, seq))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, peer: Peer, clip: Clip, seq: int) -> None:
        header, body = self._wire(clip)
        try:
            reply = await self._link.request(peer, header, body)
        except Exception:
            log.exception("clip request to %s failed", peer.device)
            reply = None
        if reply is not None or self._latest.get(peer.device) != seq:  # delivered, or superseded
            return
        for current in self._link.peers:  # the device already reconnected: send there now
            if current is not peer and current.device == peer.device and current.supports(self.name):
                self._deliver_later(current, clip)
                return
        self._pending[peer.device] = clip
        log.info("clip not delivered to %s, kept for reconnect", peer.device)

    async def on_local_text(self, text: str) -> None:
        """Local clipboard changed (must run on the event loop)."""
        text = _clean(text)
        if text:
            await self._on_local(Clip("text", text))

    async def on_local_image(self, png: bytes) -> None:
        """Local clipboard now holds an image, as PNG bytes (must run on the event loop)."""
        if png:
            await self._on_local(Clip("image", png))

    async def _on_local(self, clip: Clip) -> None:
        if self._too_big(clip):
            log.warning("local image too large, skipped (len=%d)", len(clip.data))
            return
        connected = any(p.supports(self.name) for p in self._link.peers)
        to_send = self._engine.on_local(clip, connected=connected)
        if to_send is None:
            log.debug("local %s clip dropped/queued (len=%d)", clip.kind, len(clip.data))
            return
        log.info(
            "local %s clip -> peers (len=%d, hash=%s)",
            clip.kind, len(clip.data), clip_hash(clip).hex()[:8],
        )
        await self._send_all(to_send)

    async def on_peer_connected(self, peer: Peer) -> None:
        if not peer.supports(self.name):
            return
        undelivered = self._pending.pop(peer.device, None)
        pending = self._engine.take_pending() or undelivered
        if pending is not None:
            self._deliver_later(peer, pending)

    async def on_peer_disconnected(self, peer: Peer) -> None:
        return None

    @staticmethod
    def _parse(header: dict[str, Any], body: bytes) -> Clip | None:
        if header.get("type") != "clip":
            return None
        kind = header.get("kind")
        if kind == "text":
            text = header.get("data")
            return Clip("text", text) if isinstance(text, str) and text else None
        if kind == "image":
            if header.get("mime") != PNG_MIME or not body:
                return None
            return Clip("image", bytes(body))
        return None

    async def on_message(self, peer: Peer, header: dict[str, Any], body: bytes) -> None:
        clip = self._parse(header, body)
        if clip is None:
            return
        if not self._engine.on_remote(clip):
            log.debug("remote %s clip dropped (echo), len=%d", clip.kind, len(clip.data))
            return
        log.info(
            "clip <- %s (%s, len=%d, hash=%s)",
            peer.device, clip.kind, len(clip.data), clip_hash(clip).hex()[:8],
        )
        if clip.kind == "image":
            assert isinstance(clip.data, bytes)
            try:
                path = await asyncio.to_thread(save_png, self._save_dir or default_save_dir(), clip.data)
                log.info("image saved: %s", path)
            except OSError:
                log.exception("saving image failed")
            if self._write_image is not None:
                self._write_image(clip.data)
        else:
            assert isinstance(clip.data, str)
            self._write_text(clip.data)
        await self._send_all(clip, exclude=peer)
