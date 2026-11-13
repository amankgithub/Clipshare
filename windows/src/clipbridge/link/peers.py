"""Authenticated peer and the registry of live peers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from clipbridge.link import envelope
from clipbridge.link.crypto import SecureChannel

log = logging.getLogger(__name__)

STALL_TIMEOUT = 20.0  # no write progress for this long = peer gone (PROTOCOL.md section 9)
STALL_POLL = 1.0


class TextFrame(Exception):
    """A text WebSocket message after the handshake (close 4400)."""


class Peer:
    """An authenticated connection: a client's control connection, or one of its data connections."""

    def __init__(self, device: str, ws: ServerConnection, channel: SecureChannel) -> None:
        self.device = device
        self.ws = ws
        self.channel = channel
        self._lock = asyncio.Lock()

    async def send(self, header: dict[str, Any], body: bytes = b"") -> None:
        """Encrypt and send one envelope. Raises EnvelopeTooLarge / CounterExhausted / ConnectionClosed."""
        async with self._lock:
            await self.transmit(self.seal(header, body))

    async def transmit(self, frame: bytes) -> None:
        """Send one sealed frame. If the peer accepts no bytes for STALL_TIMEOUT, abort the connection.

        Keepalive cannot catch a peer that vanished mid-send: its ping waits behind the same full
        write buffer, so without this the send (and everything queued behind it) hangs until the OS
        gives up on the TCP connection. Raises ConnectionClosed if the frame was not sent.
        """
        transport = self.ws.transport
        if transport.is_closing():  # aborted: writes would be dropped silently
            raise ConnectionClosed(None, None)
        sending = asyncio.ensure_future(self.ws.send(frame))
        lowest: int | None = None
        idle = 0.0
        aborted = False
        try:
            while not (await asyncio.wait({sending}, timeout=STALL_POLL))[0]:
                size = transport.get_write_buffer_size()
                if lowest is None or size < lowest:
                    lowest, idle = size, 0.0
                else:
                    idle += STALL_POLL
                if idle >= STALL_TIMEOUT:
                    log.warning("%s accepted no data for %.0f s; dropping it", self, idle)
                    transport.abort()
                    aborted = True
                    break
            await sending
            if aborted:  # abort resolves the pending drain without error; the frame never left
                raise ConnectionClosed(None, None)
        finally:
            sending.cancel()

    def seal(self, header: dict[str, Any], body: bytes = b"") -> bytes:
        """Encode and encrypt one envelope (advances the send counter; callers serialise sends)."""
        return self.channel.encrypt(envelope.encode(header, body))

    def open(self, message: str | bytes) -> tuple[dict[str, Any], bytes]:
        """Decrypt and decode one received message.

        Raises TextFrame (close 4400), FrameError (close 4402) or EnvelopeError (drop).
        """
        if isinstance(message, str):
            raise TextFrame
        return envelope.decode(self.channel.decrypt(message))

    def __repr__(self) -> str:
        return f"Peer({self.device})"


class PeerRegistry:
    def __init__(self) -> None:
        self._peers: dict[str, Peer] = {}

    def add(self, peer: Peer) -> Peer | None:
        """Register peer; return the older peer with the same device, if any."""
        old = self._peers.get(peer.device)
        self._peers[peer.device] = peer
        return old

    def get(self, device: str) -> Peer | None:
        return self._peers.get(device)

    def remove(self, peer: Peer) -> bool:
        """Remove only if peer is still the current one for its device."""
        if self._peers.get(peer.device) is peer:
            del self._peers[peer.device]
            return True
        return False

    def all(self) -> list[Peer]:
        return list(self._peers.values())
