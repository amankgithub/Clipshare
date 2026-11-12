"""Feature interface and the services the link gives each feature."""

from __future__ import annotations

from typing import Any, Protocol, Sequence

from clipbridge.link.peers import Peer


class Feature(Protocol):
    name: str

    async def on_peer_connected(self, peer: Peer) -> None: ...

    async def on_peer_disconnected(self, peer: Peer) -> None: ...

    async def on_message(self, peer: Peer, header: dict[str, Any], body: bytes) -> None: ...


class Sender(Protocol):
    """What a feature uses to talk to peers (implemented by the Link)."""

    @property
    def peers(self) -> Sequence[Peer]: ...

    async def send(self, peer: Peer, header: dict[str, Any], body: bytes = b"") -> bool: ...

    async def broadcast(
        self, header: dict[str, Any], body: bytes = b"", exclude: Peer | None = None
    ) -> None: ...
