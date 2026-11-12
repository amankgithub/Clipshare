"""Authenticated peer and the registry of live peers."""

from __future__ import annotations

import asyncio
from typing import Any

from websockets.asyncio.server import ServerConnection

from clipbridge.link import envelope
from clipbridge.link.crypto import SecureChannel


class Peer:
    def __init__(self, device: str, ws: ServerConnection, channel: SecureChannel) -> None:
        self.device = device
        self.ws = ws
        self.channel = channel
        self._lock = asyncio.Lock()

    async def send(self, header: dict[str, Any], body: bytes = b"") -> None:
        """Encrypt and send one envelope. Raises EnvelopeTooLarge / CounterExhausted / ConnectionClosed."""
        plaintext = envelope.encode(header, body)
        async with self._lock:
            await self.ws.send(self.channel.encrypt(plaintext))

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

    def remove(self, peer: Peer) -> bool:
        """Remove only if peer is still the current one for its device."""
        if self._peers.get(peer.device) is peer:
            del self._peers[peer.device]
            return True
        return False

    def all(self) -> list[Peer]:
        return list(self._peers.values())
